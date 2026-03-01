# Morning Brief (Gmail Zillow/Redfin)

## What this does
Runs daily at **7:30am America/New_York**:
- pulls last 24h of Gmail emails matching (Zillow OR Redfin)
- best-effort parses listings
- filters to: 3–4 bed, 2+ bath, <= $600k, ZIPs 07306/07304/07305/07105/07047
- runs cashflow with defaults
- emails the brief to **budtechai@gmail.com**
- saves a copy to: `~/.openclaw/workspace/morning_brief/latest_brief.md`

## 1) Create Google OAuth credentials (Desktop app)
Google Cloud Console:
- APIs & Services → Credentials → Create Credentials → OAuth client ID
- Application type: **Desktop app**
- Download JSON and save as:
  `~/.openclaw/workspace/morning_brief/credentials.json`

Enable Gmail API for the project.

## 2) Install deps
From this repo/workspace:

```bash
python3 -m venv ~/.openclaw/workspace/morning_brief/.venv
source ~/.openclaw/workspace/morning_brief/.venv/bin/activate
pip install -r ~/.openclaw/workspace/morning_brief/requirements.txt
```

## 3) First run (interactive OAuth)
```bash
source ~/.openclaw/workspace/morning_brief/.venv/bin/activate
python ~/.openclaw/workspace/morning_brief/scripts/morning_brief.py --to budtechai@gmail.com
```

This will open a browser window to authorize and will write:
- token: `~/.openclaw/workspace/morning_brief/token.json`

## 4) Cron install
Edit crontab:
```bash
crontab -e
```
Add:
```cron
TZ=America/New_York
30 7 * * * ~/.openclaw/workspace/morning_brief/.venv/bin/python ~/.openclaw/workspace/morning_brief/scripts/morning_brief.py --to budtechai@gmail.com >> ~/.openclaw/workspace/morning_brief/cron.log 2>&1
```

## Notes / next step
Parsing is intentionally conservative right now. To make this accurate, paste 1 sample Zillow alert + 1 sample Redfin alert (redacted) so we can extract price/beds/baths/zip reliably per listing.
