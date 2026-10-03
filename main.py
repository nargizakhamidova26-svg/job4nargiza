"""
Daily job scraper for Nargiza Khamidova.
Uses ReliefWeb's public API to pull UN agency, NGO, and
foundation job postings, filters through Claude Haiku,
sends matches to Telegram.
"""

import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup

# -------------------------------------------------------------
# Config
# -------------------------------------------------------------

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

MIN_SCORE = 7
SEEN_FILE = Path("data/seen_jobs.json")
MODEL = "claude-haiku-4-5-20251001"

UNJOBS_URL = "https://unjobs.org/new"
JOBS_PER_FETCH = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# collect diagnostic messages to send to Telegram at the end
DIAGNOSTICS = []

def log(msg):
    print(msg)
    DIAGNOSTICS.append(msg)

# -------------------------------------------------------------
# Filter prompt
# -------------------------------------------------------------

FILTER_PROMPT = """
You are a job-search filtering agent for Nargiza Khamidova.
Your task: given a list of vacancy postings, return ONLY the
roles that match her profile. Score each match 1-10 and
include a one-sentence reason.

CANDIDATE PROFILE

Current role: Programme Analyst, Inclusive Growth, UNDP
Uzbekistan (FTA, NOA/7, since Sep 2024). Manages USD 5M+
annual portfolio.

Experience: 5+ years across UNDP, OSCE, UN DESA, KIPA Seoul,
East Telecom (national telecommunications operator).

Education: MPP, KDI School of Public Policy and Management
(Global Korea Scholarship, 2020). BA International Economic
Relations, UWED Tashkent.

Languages: English (proficient), Russian (proficient),
Uzbek (native), Korean (professional).

Thematic expertise:
- Digital economy, digital inclusion, digital public infrastructure
- Women's economic empowerment, women's entrepreneurship
- Financial inclusion, inclusive finance
- Multidimensional poverty measurement (MPI, Alkire-Foster)
- AI policy, AI regulatory sandboxes, AI ethics
- Social protection reform
- Inclusive growth, startup ecosystems
- MEL, results-based management
- Data analytics (R, Python, Tableau, Power BI)

TARGET LEVEL
- P-2, P-3 (UN grade)
- IPSA-9, IPSA-10 (UNDP)
- Programme Officer, Programme Analyst, Programme Specialist
- Programme Manager (foundation sector)
- Analyst, Officer, Specialist at DFIs
- NOT director-tier or 10+ years required
- NOT internships or PhD-only roles

TARGET LOCATIONS
Preferred: Europe (Geneva, Bonn, Rome, Helsinki, Stockholm,
London, Brussels, Leiden, The Hague, Berlin, Zurich, Basel,
Copenhagen), Korea (Seoul, Incheon, Songdo), stable
Asia-Pacific duty stations (Bangkok, Singapore).

Acceptable: HQ New York / DC roles ONLY if explicitly
international staff with G-4 visa sponsorship.

EXCLUDE
- Uzbekistan and Central Asia (current location)
- Roles stating "no visa sponsorship" in US/UK
- Roles requiring African context operational experience
- Roles requiring 10+ years
- Director, Head, VP, Chief tier
- Pure corporate CSR / brand roles
- Pure engineering / software developer roles
- Roles requiring Spanish or French as required language
- Fundraising-only or communications-only roles
- Climate science / health science research roles
- Humanitarian field roles in active conflict zones
- Driver, admin assistant, support staff roles

SCORING RUBRIC

Score each role 1-10 against:
1. Level match (grade, years required)
2. Thematic fit
3. Location acceptability
4. Contract shape (FTA > TA > IPSA > consultancy)
5. Visa feasibility for Uzbek national

Score strictly. Most roles should score 3-5. Reserve 7+
for genuine fits.

OUTPUT FORMAT

Return a JSON array of ONLY the matching roles with score >= 7.
Each object:
{
  "title": "...",
  "organization": "...",
  "location": "...",
  "deadline": "...",
  "score": N,
  "reason": "one sentence",
  "url": "..."
}

Return [] if no matches. Return ONLY the JSON array, no other text.
"""

# -------------------------------------------------------------
# Scraper - UNjobs.org (reliable HTML aggregator)
# -------------------------------------------------------------

def scrape_unjobs():
    """Scrape newest listings from UNjobs.org."""
    jobs = []

    # UNjobs paginates: /new, /new/2, /new/3 ...
    # Each page has ~50 jobs. Pull first 2 pages = ~100 newest.
    pages = [UNJOBS_URL, f"{UNJOBS_URL}/2"]

    for page_url in pages:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=30)
            log(f"[unjobs] {page_url} HTTP {resp.status_code}")
            if resp.status_code != 200:
                log(f"[unjobs] body: {resp.text[:200]}")
                continue

            soup = BeautifulSoup(resp.text, "lxml")

            # UNjobs job cards are <div class="job"> containers
            cards = soup.select("div.job")
            if not cards:
                # fallback: any link to a vacancy page
                cards = soup.select("a[href*='/vacancies/']")

            for card in cards:
                # Title + link
                link_el = card.find("a", href=True) if card.name == "div" else card
                if not link_el:
                    continue
                href = link_el.get("href", "")
                if not href:
                    continue
                url = href if href.startswith("http") else f"https://unjobs.org{href}"

                title = link_el.get_text(strip=True)
                if not title or len(title) < 5:
                    continue

                # Full card text has org, location, deadline inline
                card_text = card.get_text(" ", strip=True) if card.name == "div" else title
                card_text = card_text[:400]

                # UNjobs format: "Title | Organization, Location — Deadline"
                org = ""
                location = ""
                deadline = ""

                # Try to pull org from <p> or next sibling
                meta_el = card.find("p") if card.name == "div" else None
                if meta_el:
                    meta_text = meta_el.get_text(" ", strip=True)
                    # Format often: "UNDP, Geneva Switzerland | Closing: 15 Nov 2026"
                    if "|" in meta_text:
                        left, right = meta_text.split("|", 1)
                        if "," in left:
                            parts = left.split(",", 1)
                            org = parts[0].strip()
                            location = parts[1].strip()
                        else:
                            org = left.strip()
                        if "Closing" in right or "Deadline" in right:
                            deadline = right.replace("Closing:", "").replace("Deadline:", "").strip()
                        else:
                            deadline = right.strip()
                    elif "," in meta_text:
                        parts = meta_text.split(",", 1)
                        org = parts[0].strip()
                        location = parts[1].strip()
                    else:
                        org = meta_text

                jobs.append({
                    "title": title,
                    "organization": org[:120],
                    "location": location[:120],
                    "deadline": deadline[:40],
                    "description": card_text,
                    "url": url,
                    "source": "unjobs",
                })

                if len(jobs) >= JOBS_PER_FETCH:
                    break

            if len(jobs) >= JOBS_PER_FETCH:
                break

        except Exception as e:
            log(f"[unjobs] EXCEPTION on {page_url}: {e}")
            traceback.print_exc()

    log(f"[unjobs] parsed {len(jobs)} jobs")
    return jobs

# -------------------------------------------------------------
# Dedup
# -------------------------------------------------------------

def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text() or "[]"))
    return set()


def save_seen(seen):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


def filter_unseen(jobs, seen):
    return [j for j in jobs if j["url"] and j["url"] not in seen]

# -------------------------------------------------------------
# Claude filter
# -------------------------------------------------------------

def filter_with_claude(jobs):
    if not jobs:
        return []

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    matches = []

    for i in range(0, len(jobs), 20):
        batch = jobs[i:i + 20]
        payload = json.dumps(
            [{
                "title": j["title"],
                "organization": j.get("organization", ""),
                "location": j.get("location", ""),
                "deadline": j.get("deadline", ""),
                "description": j.get("description", "")[:600],
                "url": j["url"],
            } for j in batch],
            ensure_ascii=False,
        )

        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=4000,
                messages=[{
                    "role": "user",
                    "content": f"{FILTER_PROMPT}\n\nJOBS TO FILTER:\n{payload}",
                }],
            )
            text = msg.content[0].text.strip()
            text = re.sub(r"^```(?:json)?|```$", "", text,
                          flags=re.MULTILINE).strip()
            parsed = json.loads(text)
            for item in parsed:
                if item.get("score", 0) >= MIN_SCORE:
                    matches.append(item)
        except Exception as e:
            log(f"[filter] batch {i} failed: {e}")
            continue

    log(f"[filter] {len(matches)} matches at score >= {MIN_SCORE}")
    return matches

# -------------------------------------------------------------
# Telegram
# -------------------------------------------------------------

def send_telegram(job):
    title = job.get("title", "")
    org = job.get("organization", "")
    location = job.get("location", "")
    deadline = job.get("deadline", "")
    score = job.get("score", "?")
    reason = job.get("reason", "")
    url = job.get("url", "")

    text = (
        f"Score {score}/10 - {title}\n"
        f"{org} | {location}\n"
        f"Deadline: {deadline}\n"
        f"Why: {reason}\n"
        f"{url}"
    )

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": False,
            },
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"[telegram] send failed: {e}", file=sys.stderr)


def send_summary(total_raw, total_new, total_matched):
    diag_text = "\n".join(DIAGNOSTICS[-10:]) if DIAGNOSTICS else "(no logs)"
    text = (
        f"Daily scrape - {datetime.utcnow():%Y-%m-%d %H:%M} UTC\n"
        f"Scraped: {total_raw}\n"
        f"New (unseen): {total_new}\n"
        f"Matches (score >= {MIN_SCORE}): {total_matched}\n"
        f"\nDiagnostics:\n{diag_text}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"[telegram] summary failed: {e}", file=sys.stderr)

# -------------------------------------------------------------
# Main
# -------------------------------------------------------------

def main():
    seen = load_seen()
    log(f"[main] seen store has {len(seen)} URLs")

    raw = scrape_unjobs()
    new = filter_unseen(raw, seen)
    log(f"[main] {len(new)} new jobs after dedup")

    matches = filter_with_claude(new)

    for job in matches:
        send_telegram(job)

    for job in new:
        if job["url"]:
            seen.add(job["url"])
    save_seen(seen)

    send_summary(len(raw), len(new), len(matches))


if __name__ == "__main__":
    main()
