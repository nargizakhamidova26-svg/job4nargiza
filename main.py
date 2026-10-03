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

MIN_SCORE = 5
SEEN_FILE = Path("data/seen_jobs.json")
JOBS_FILE = Path("docs/data/jobs.json")  # served by GitHub Pages
MODEL = "claude-haiku-4-5-20251001"

UNJOBS_URL = "https://unjobs.org/new"
JOBS_PER_FETCH = 150

# Foundation-specific pages on unjobs.org
FOUNDATION_PAGES = [
    "https://unjobs.org/organizations/the-mastercard-foundation",
    "https://unjobs.org/organizations/open-society-foundations",
    "https://unjobs.org/organizations/aga-khan-foundation",
    "https://unjobs.org/organizations/ikea-foundation",
    "https://unjobs.org/organizations/children-s-investment-fund-foundation-ciff",
    "https://unjobs.org/organizations/ford-foundation",
    "https://unjobs.org/organizations/rockefeller-foundation",
    "https://unjobs.org/organizations/bill-melinda-gates-foundation",
    "https://unjobs.org/organizations/wellcome-trust",
    "https://unjobs.org/organizations/macarthur-foundation",
    "https://unjobs.org/organizations/oak-foundation",
    "https://unjobs.org/organizations/luminate",
    "https://unjobs.org/organizations/co-impact",
]

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

EXCLUDE (score 1-3 for any of these)
- NATIONAL positions — National Officer (NO-A, NO-B, NO-C, NO-D),
  Service Band (SB-3, SB-4, SB-5), National UN Volunteer (NUNV),
  National Consultant, National Programme Officer, or any role with
  "National" or "(National)" in the title. She wants INTERNATIONAL only.
- Uzbekistan, Tajikistan, Kyrgyzstan, Kazakhstan, Turkmenistan,
  Afghanistan (Central Asia region — she wants OUT)
- Roles stating "no visa sponsorship" in US/UK
- Roles requiring African context operational experience
- Roles requiring 10+ years experience
- Director, Head, VP, Chief, Lead tier
- Pure corporate CSR / brand roles
- Pure engineering / software developer / IT support roles
- Roles requiring Spanish or French as REQUIRED language
- Fundraising-only or communications-only roles
- Climate science / health science research roles
- Humanitarian field roles in active conflict zones
- Driver, admin assistant, finance clerk, procurement assistant, support staff

SCORING RUBRIC

Score each role 1-10 against:
1. Level match (grade, years required)
2. Thematic fit
3. Location acceptability
4. Contract shape (FTA > TA > IPSA > consultancy)
5. Visa feasibility for Uzbek national

IMPORTANT: These listings come from an aggregator and often
only include title + organization + location, no description.
When info is thin, judge on title + organization + location
alone, and give BENEFIT OF DOUBT for roles that look like
they could match. Don't downscore just because description
is short.

- Clear thematic match + acceptable location → score 7-9
- Right level + right agency + unclear theme → score 5-6
- Wrong level, wrong location, or clearly off-theme → score 1-4

Return anything scoring 5 or higher.

OUTPUT FORMAT

Each input job has: title, raw_text (containing organization, location, grade,
deadline etc.), url.

EXTRACT the organization, location, and deadline from raw_text yourself.
UNjobs format usually puts them pipe-separated or comma-separated near the title.

Return a JSON array of ONLY the matching roles with score >= 5.
Each object MUST include ALL fields (use "Not specified" ONLY if truly absent):

{
  "title": "position title",
  "organization": "name of organization (e.g. UNDP, UNICEF, Mastercard Foundation)",
  "location": "city, country",
  "deadline": "closing date in format DD Mon YYYY if findable",
  "score": N,
  "reason": "one sentence why it fits",
  "url": "..."
}

Return [] if no matches. Return ONLY the JSON array, no other text.
"""

# -------------------------------------------------------------
# Scraper - UNjobs.org (reliable HTML aggregator)
# -------------------------------------------------------------

def parse_unjobs_page(url, source_label):
    """Parse a single UNjobs listing page and return list of job dicts."""
    jobs = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        log(f"[{source_label}] {url} HTTP {resp.status_code}")
        if resp.status_code != 200:
            return jobs

        soup = BeautifulSoup(resp.text, "lxml")
        cards = soup.select("div.job")
        if not cards:
            cards = soup.select("article") or soup.select("a[href*='/vacancies/']")

        for card in cards:
            link_el = card.find("a", href=True) if card.name == "div" else card
            if not link_el:
                continue
            href = link_el.get("href", "")
            if not href:
                continue
            job_url = href if href.startswith("http") else f"https://unjobs.org{href}"

            title = link_el.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            # Capture ALL text from the card - Claude will extract fields
            card_text = card.get_text(" | ", strip=True) if card.name == "div" else title
            card_text = card_text[:600]

            jobs.append({
                "title": title,
                "organization": "",   # Claude extracts from description
                "location": "",       # Claude extracts
                "deadline": "",       # Claude extracts
                "description": card_text,
                "url": job_url,
                "source": source_label,
            })
    except Exception as e:
        log(f"[{source_label}] EXCEPTION on {url}: {e}")
        traceback.print_exc()
    return jobs


def scrape_unjobs():
    """Scrape UN agency jobs + foundation-specific pages."""
    jobs = []

    # Main UN jobs feed - 3 pages = ~150 newest
    for page_url in [UNJOBS_URL, f"{UNJOBS_URL}/2", f"{UNJOBS_URL}/3"]:
        jobs.extend(parse_unjobs_page(page_url, "unjobs-new"))
        if len(jobs) >= JOBS_PER_FETCH:
            break

    # Foundation-specific pages - pull all open roles from each
    foundation_jobs = []
    for foundation_url in FOUNDATION_PAGES:
        foundation_jobs.extend(parse_unjobs_page(foundation_url, "unjobs-foundation"))

    log(f"[unjobs] main feed: {len(jobs)}, foundations: {len(foundation_jobs)}")

    # Dedupe within this run by URL
    seen_urls = {j["url"] for j in jobs}
    for j in foundation_jobs:
        if j["url"] not in seen_urls:
            jobs.append(j)
            seen_urls.add(j["url"])

    log(f"[unjobs] total unique: {len(jobs)}")
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
                "raw_text": j.get("description", "")[:600],
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
            # Extract just the JSON array - ignore any text before [ or after ]
            match = re.search(r"\[.*\]", text, flags=re.DOTALL)
            if match:
                text = match.group(0)
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
    title = job.get("title", "Not specified")
    org = job.get("organization", "Not specified")
    location = job.get("location", "Not specified")
    deadline = job.get("deadline", "Not specified")
    score = job.get("score", "?")
    reason = job.get("reason", "")
    url = job.get("url", "")

    text = (
        f"⭐ Score: {score}/10\n\n"
        f"Position: {title}\n"
        f"Organization: {org}\n"
        f"Location: {location}\n"
        f"Deadline: {deadline}\n\n"
        f"Why it fits: {reason}\n\n"
        f"Link: {url}"
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

def append_to_dashboard(matches):
    """Append new matches to docs/data/jobs.json for the dashboard."""
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if JOBS_FILE.exists():
        existing = json.loads(JOBS_FILE.read_text() or "[]")
    else:
        existing = []

    existing_urls = {j.get("url", "") for j in existing}
    today = datetime.utcnow().strftime("%Y-%m-%d")

    for m in matches:
        if m.get("url") and m["url"] not in existing_urls:
            m["found_date"] = today
            existing.append(m)
            existing_urls.add(m["url"])

    # Keep newest-first
    existing.sort(key=lambda j: j.get("found_date", ""), reverse=True)
    JOBS_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    log(f"[dashboard] {len(existing)} total tracked jobs")


def main():
    seen = load_seen()
    log(f"[main] seen store has {len(seen)} URLs")

    raw = scrape_unjobs()
    new = filter_unseen(raw, seen)
    log(f"[main] {len(new)} new jobs after dedup")

    matches = filter_with_claude(new)

    for job in matches:
        send_telegram(job)

    append_to_dashboard(matches)

    for job in new:
        if job["url"]:
            seen.add(job["url"])
    save_seen(seen)

    send_summary(len(raw), len(new), len(matches))


if __name__ == "__main__":
    main()
