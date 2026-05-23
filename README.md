# Flight Price Alerts ✈️

Monitors round-trip economy fares from **AMS / BRU / EIN / RTM / DUS / CRL / CDG** → **DEL / BOM / CCU** for the Dec 26 2026 → Jan 7 2027 outbound and Feb 22 → Mar 8 2027 return windows. Posts a daily summary plus instant alerts for price drops and threshold crossings to Slack.

Runs every 6 hours on GitHub Actions. Uses **SerpAPI's Google Flights** engine for real-time prices.

---

## How it works (rotating-grid strategy)

SerpAPI's `google_flights` engine takes one `outbound_date` + one `return_date` per call (no native flexible-date support), but accepts **comma-separated** `departure_id` and `arrival_id` — so one call covers all 7 origins × 3 destinations for that date pair.

To cover a flexible window inside the free tier, the script samples a grid:

- 6 outbound dates × 4 return dates = **24 date combos**
- Each run consumes `CALLS_PER_RUN = 2` searches and advances a rotating index
- 4 runs/day × 2 = **8 searches/day = ~240/month** (free tier is 250/month)
- Full 24-combo refresh cycle completes every ~3 days
- State (`state.json`) keeps the cheapest price per `(route, outbound_date, return_date)` so drop alerts compare apples to apples

After ~3 days the grid is fully populated and the daily summary reflects the cheapest known combo across the entire window.

---

## Configuration at a glance

Edit the constants at the top of `flight_alerts.py`:

| Constant | Default | Meaning |
|---|---|---|
| `ORIGINS` | `[AMS, BRU, EIN, RTM, DUS, CRL, CDG]` | Origin IATAs (comma-joined in one API call) |
| `DESTINATIONS` | `[DEL, BOM, CCU]` | Destination IATAs |
| `OUTBOUND_DATES` | 6 sampled dates in window | The "rows" of the rotation grid |
| `RETURN_DATES` | 4 sampled dates in window | The "columns" of the rotation grid |
| `CALLS_PER_RUN` | `2` | SerpAPI calls per cron tick. Raise it to refresh faster if you upgrade your plan |
| `STOPS_CODE` | `2` | `0`=any, `1`=nonstop, `2`=≤1 stop, `3`=≤2 stops |
| `PRICE_THRESHOLD_EUR` | `700` | Alert fires when a refreshed combo crosses below this |
| `DROP_PCT_THRESHOLD` | `5.0` | Min % drop vs the previous price of that same combo |
| `DAILY_SUMMARY_UTC_HOURS` | `{6}` | Cron hour(s) when the full summary block is sent. Other runs are silent unless alerts fire. |

---

## Setup (~5 minutes)

### 1. SerpAPI key

You already have one with 250 free searches/month. Find it at [serpapi.com/manage-api-key](https://serpapi.com/manage-api-key).

### 2. Push this repo to GitHub

```bash
git init flight-alerts && cd flight-alerts
# copy the files in
git add . && git commit -m "init flight alerts"
git remote add origin git@github.com:<you>/flight-alerts.git
git push -u origin main
```

### 3. Add repository secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `SERPAPI_KEY` | Your SerpAPI key |
| `SLACK_WEBHOOK_URL` | Your Slack incoming webhook URL |

### 4. First run

Go to **Actions → Flight Price Alerts → Run workflow**. The first run posts a "first run — current snapshot" message with whatever it found in the first 2 date combos. After ~12 runs (3 days) the grid will be fully populated.

---

## What you'll see in Slack

| Run type | Behavior |
|---|---|
| **First run** (state empty) | Posts a snapshot of the 2 combos checked. No drop/threshold alerts (no history). |
| **Summary hour** (06:00 UTC by default) | Posts the full daily summary across all known combos in state, plus any alerts. |
| **Other runs** (00, 12, 18 UTC) | Silent unless the refreshed combos crossed the price threshold or dropped ≥ 5% vs the previous price of that same combo. |

---

## Run locally (dry-run)

```bash
export SERPAPI_KEY=...
python flight_alerts.py --dry-run
```

`--dry-run` prints the Slack payload to stdout instead of posting. `state.json` is still written, so you'll advance the rotation cursor — reset it manually if you don't want that.

---

## Tuning your SerpAPI budget

| Goal | Setting |
|---|---|
| Use less of your free quota | Drop `CALLS_PER_RUN` to `1` → 4/day = 120/mo; full cycle every 6 days |
| Refresh the grid faster | Upgrade SerpAPI plan, raise `CALLS_PER_RUN` |
| Add more sampled dates | Add entries to `OUTBOUND_DATES` / `RETURN_DATES` — refresh cycle stretches accordingly |
| Quieter Slack | Empty `DAILY_SUMMARY_UTC_HOURS = set()` to keep only alerts |

---

## Notes & caveats

- **Per-combo drop alerts**: drops are detected when a freshly-fetched combo is ≥5% cheaper than the previous price *for that same combo*, not across the whole grid. This avoids false positives from comparing a cheap Tuesday to an expensive Friday.
- **Threshold alerts** fire on the first observation below €700 (`old is None or old > threshold`). To suppress these on launch, set a `last_run` timestamp manually in `state.json` before pushing.
- **Round-trip stops**: SerpAPI's `stops=2` filter applies to both directions, so all results respect the ≤1-stop constraint without an extra filter step.
- **Links** in Slack messages deep-link to Google Flights' own search UI for that route — useful for cross-checking before booking.
- **Quota awareness**: if you ever bust 250/month, SerpAPI returns an error and the script logs it; nothing gets persisted for that call. Monthly quota resets at the start of each calendar month.

---

## Files

```
flight-alerts/
├── .github/workflows/flight-alerts.yml   # cron + manual trigger
├── flight_alerts.py                       # main script
├── requirements.txt                       # just requests
├── state.json                             # rotation cursor + per-combo cheapest (committed)
└── README.md                              # this file
```
