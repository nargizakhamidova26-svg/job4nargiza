# job4nargiza

Daily job scraper for philanthropy and UN development sector roles.

## How it works

1. GitHub Actions runs `main.py` daily at 06:00 Tashkent time (01:00 UTC)
2. Scrapes Devex job listings
3. Deduplicates against `data/seen_jobs.json`
4. Sends new jobs through Claude Haiku with a filter prompt tailored to the candidate profile
5. Posts matches scoring 7+ to Telegram
6. Commits updated `seen_jobs.json` back to the repo

## Setup

### GitHub Secrets required

In repo Settings -> Secrets and variables -> Actions, add:

- `TELEGRAM_TOKEN` - from BotFather
- `TELEGRAM_CHAT_ID` - from @userinfobot
- `ANTHROPIC_API_KEY` - from console.anthropic.com

### GitHub Actions permissions

Settings -> Actions -> General -> Workflow permissions -> **Read and write permissions** -> Save

(Needed so the workflow can commit updated `seen_jobs.json` back.)

### First run

Go to Actions tab -> "Daily job scrape" -> "Run workflow"

## Files

- `main.py` - scrape, filter, notify pipeline
- `requirements.txt` - Python dependencies
- `.github/workflows/scrape.yml` - daily cron schedule
- `data/seen_jobs.json` - dedup store
- `.gitignore` - excludes .env and local Python cache

## Tuning

If too many weak matches: tighten the EXCLUDE list in `FILTER_PROMPT` inside `main.py`.
If missing obvious good matches: loosen criteria or add example roles.

## Adding more scrapers

Add a new function like `scrape_impactpool()` returning the same job dict shape, then call it from `main()` and concatenate results with `scrape_devex()`.
