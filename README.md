# ⚡ Elite Stock Scanner

Multi-source 7-layer conviction scoring system for finding explosive stocks **before** they move.

## What This Does

Scans the entire stock market daily and ranks each stock through **7 layers of conviction**:

| Layer | Max Pts | What It Detects |
|---|---|---|
| 📅 **Catalyst** | 25 | Upcoming earnings, FDA dates |
| 🧨 **Squeeze** | 20 | High short interest + low float |
| 💰 **Smart Money** | 15 | Institutional accumulation patterns |
| 📞 **Options** | 15 | Unusual options activity |
| 🐦 **Social** | 10 | Reddit/WSB mention velocity |
| 💪 **Strength** | 10 | Relative strength vs SPY |
| 📈 **Technical** | 5 | Breakout setup |

**Total: 100 points**

## Tier System

- **⭐ Tier S (75+)** — Highest conviction, multi-layer alignment
- **🟢 Tier 1 (60-74)** — Strong setup, multiple confirmations
- **🔵 Tier 2 (45-59)** — Worth watching
- **⚪ Tier 3 (35-44)** — Honorable mention

## Setup

1. **Fork or copy** this repo to your GitHub account
2. Go to **Settings → Actions → General → Workflow permissions** and select "Read and write permissions"
3. Go to **Settings → Pages → Source: Deploy from branch → main / root**
4. Wait 60 seconds, then visit `https://YOUR_USERNAME.github.io/REPO_NAME/dashboard.html`

## Schedule

The scanner runs automatically:
- 9:00 AM ET (8:00 PM Bangkok) — Pre-market scan
- 10:30 AM ET (9:30 PM Bangkok) — Mid-morning update
- 1:00 PM ET (12:00 AM Bangkok) — Afternoon update

## Files

- `elite_scanner.py` — Main scoring engine
- `elite_dashboard.py` — Dashboard builder
- `.github/workflows/scanner.yml` — Automation schedule
- `elite_watchlist.csv` — All ranked stocks
- `elite_watchlist.json` — Top 15 (used by dashboard)
- `dashboard.html` — Live UI

## Manual Run

Click **Actions → Elite Scanner → Run workflow** to scan immediately.

## Local Testing

```bash
pip install -r requirements.txt
python elite_scanner.py
python elite_dashboard.py
# Open dashboard.html in browser
```

## Cost

$0/month. Fully automated. Runs in the cloud.
