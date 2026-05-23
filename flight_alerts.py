#!/usr/bin/env python3
"""
Flight price alerts via SerpAPI Google Flights.

Strategy: 24 date combos (6 outbound × 4 return) covered by a rotating window.
Each run consumes CALLS_PER_RUN searches; with 4 runs/day × 2 calls = 8/day = 240/month,
fitting the 250-search SerpAPI free tier with ~10 calls of headroom.

Per call we hit *all* 7 origins and 3 destinations at once (comma-separated),
so each call yields up to 21 route prices for one (outbound, return) date pair.

A full 24-combo refresh cycle completes every ~3 days. State persists per
(route, date-combo) so drop alerts compare apples to apples.
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ============================== CONFIG (edit me) ==============================
ORIGINS: list[str] = ["AMS", "BRU", "EIN", "RTM", "DUS", "CRL", "CDG"]
DESTINATIONS: list[str] = ["DEL", "BOM", "CCU"]

# Sampled outbound dates within Dec 26, 2026 – Jan 7, 2027 (6 dates, ~2 days apart)
OUTBOUND_DATES: list[str] = [
    "2026-12-26", "2026-12-28", "2026-12-30",
    "2027-01-01", "2027-01-04", "2027-01-07",
]

# Sampled return dates within Feb 22 – Mar 8, 2027 (4 dates, ~3-5 days apart)
RETURN_DATES: list[str] = [
    "2027-02-22", "2027-02-25", "2027-02-28", "2027-03-05",
]

# Calls per run × runs/day must stay under (SerpAPI free tier / ~30)
# 2 × 4 = 8/day = 240/month → fits 250 free tier with 10 buffer
CALLS_PER_RUN: int = 2

PASSENGERS: int = 1
CURRENCY: str = "EUR"
TRAVEL_CLASS_CODE: int = 1     # 1=Economy 2=Premium 3=Business 4=First
STOPS_CODE: int = 2            # 0=any 1=nonstop 2=≤1stop 3=≤2stops

# Alerting
PRICE_THRESHOLD_EUR: float = 700.0
DROP_PCT_THRESHOLD: float = 5.0
TOP_N_SUMMARY: int = 5

# Send the full daily summary at this UTC hour. With cron "0 */6 * * *"
# the runs land at 00, 06, 12, 18 UTC. 06 UTC = morning Europe.
DAILY_SUMMARY_UTC_HOURS: set[int] = {6}

# Misc
STATE_FILE = Path("state.json")
API_BASE = "https://serpapi.com/search"
HTTP_TIMEOUT_SEC = 60
INTER_CALL_DELAY_SEC = 1.0
# ==============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("flight-alerts")


# ---------------------------- SerpAPI ----------------------------
def search_flights(api_key: str, outbound_date: str, return_date: str) -> list[dict]:
    """One SerpAPI call covers all 7 origins × 3 destinations for one date pair."""
    params = {
        "engine": "google_flights",
        "api_key": api_key,
        "departure_id": ",".join(ORIGINS),
        "arrival_id": ",".join(DESTINATIONS),
        "outbound_date": outbound_date,
        "return_date": return_date,
        "currency": CURRENCY,
        "adults": PASSENGERS,
        "travel_class": TRAVEL_CLASS_CODE,
        "stops": STOPS_CODE,
        "type": 1,        # 1 = round-trip
        "hl": "en",
    }
    try:
        r = requests.get(API_BASE, params=params, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning("SerpAPI HTTP error (%s/%s): %s", outbound_date, return_date, e)
        return []
    except ValueError as e:
        log.warning("SerpAPI bad JSON (%s/%s): %s", outbound_date, return_date, e)
        return []

    if "error" in data:
        log.warning("SerpAPI error for %s/%s: %s", outbound_date, return_date, data["error"])
        return []

    offers: list[dict] = []
    for f in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        legs = f.get("flights") or []
        if not legs or f.get("price") is None:
            continue
        origin = (legs[0].get("departure_airport") or {}).get("id", "")
        dest = (legs[-1].get("arrival_airport") or {}).get("id", "")
        if origin not in ORIGINS or dest not in DESTINATIONS:
            continue
        offers.append({
            "origin": origin,
            "destination": dest,
            "price": float(f["price"]),
            "outbound_date": outbound_date,
            "return_date": return_date,
            "airline": legs[0].get("airline", ""),
            "stops_out": len(legs) - 1,
            "total_duration_min": f.get("total_duration"),
            "link": _google_flights_link(origin, dest, outbound_date, return_date),
        })
    return offers


def _google_flights_link(origin: str, dest: str, outbound: str, ret: str) -> str:
    """Deep link into Google Flights for that specific route + dates."""
    q = f"Flights from {origin} to {dest} on {outbound} returning {ret}"
    return "https://www.google.com/travel/flights?q=" + requests.utils.quote(q)


# ---------------------------- helpers ----------------------------
def make_key(origin: str, dest: str, outbound: str, ret: str) -> str:
    return f"{origin}-{dest}|{outbound}|{ret}"


def all_combos() -> list[tuple[str, str]]:
    return [(o, r) for o in OUTBOUND_DATES for r in RETURN_DATES]


def get_combos_for_run(rotation_index: int) -> tuple[list[tuple[str, str]], int]:
    """Pick the next CALLS_PER_RUN combos; wrap around the grid."""
    combos = all_combos()
    n = len(combos)
    selected = [combos[(rotation_index + i) % n] for i in range(CALLS_PER_RUN)]
    next_index = (rotation_index + CALLS_PER_RUN) % n
    return selected, next_index


def reduce_to_cheapest(offers: list[dict]) -> dict[str, dict]:
    """Keep cheapest offer per (origin, dest, outbound, return) key."""
    out: dict[str, dict] = {}
    for o in offers:
        k = make_key(o["origin"], o["destination"], o["outbound_date"], o["return_date"])
        if k not in out or o["price"] < out[k]["price"]:
            out[k] = o
    return out


# ---------------------------- state ----------------------------
def load_state() -> dict[str, Any]:
    """Load state, migrating gracefully from any older schema."""
    default = {"last_run": None, "rotation_index": 0, "prices": {}}
    if not STATE_FILE.exists():
        return default
    try:
        s = json.loads(STATE_FILE.read_text())
    except Exception as e:
        log.warning("State load failed (%s); starting fresh.", e)
        return default
    if "prices" not in s:
        log.info("Old state schema detected; resetting.")
        return default
    s.setdefault("rotation_index", 0)
    s.setdefault("prices", {})
    return s


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ---------------------------- Slack message ----------------------------
def fmt_line(o: dict) -> str:
    stops_n = o.get("stops_out", 0) or 0
    stops_str = "direct" if stops_n == 0 else f"{stops_n} stop{'s' if stops_n > 1 else ''}"
    airline = f" {o['airline']}" if o.get("airline") else ""
    link = o.get("link") or ""
    link_part = f" — <{link}|view>" if link else ""
    return (
        f"• *{o['origin']}→{o['destination']}* — *€{int(o['price'])}* "
        f"({stops_str}{airline}) — {o['outbound_date']} → {o['return_date']}{link_part}"
    )


def build_message(
    refreshed: dict[str, dict],
    state_prices: dict[str, dict],
    state_prices_before: dict[str, dict],
    is_first_run: bool,
    is_summary_hour: bool,
) -> dict | None:
    # ---- compute alerts (only on combos we just refreshed) ----
    threshold_alerts: list[tuple[str, dict, dict | None]] = []
    drop_alerts: list[tuple[str, dict, dict, float]] = []
    if not is_first_run:
        for k, new in refreshed.items():
            old = state_prices_before.get(k)
            # Threshold crossing (newly below threshold)
            if new["price"] <= PRICE_THRESHOLD_EUR and (old is None or old["price"] > PRICE_THRESHOLD_EUR):
                threshold_alerts.append((k, new, old))
            # Significant drop vs the previous price for THIS combo
            if old and old.get("price"):
                pct = (old["price"] - new["price"]) / old["price"] * 100
                if pct >= DROP_PCT_THRESHOLD:
                    drop_alerts.append((k, new, old, pct))

    has_alerts = bool(threshold_alerts or drop_alerts)
    if not has_alerts and not is_summary_hour and not is_first_run:
        return None

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    date_combos_covered = len({(o["outbound_date"], o["return_date"]) for o in state_prices.values()})
    total_combos = len(all_combos())

    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "✈️  Flight price check — Europe → India"}},
        {"type": "context",
         "elements": [{
             "type": "mrkdwn",
             "text": (
                 f"_{now_str}_  •  _via Google Flights (SerpAPI)_  •  "
                 f"_Refreshed this run:_ {len(refreshed)} routes across "
                 f"{len({(o['outbound_date'], o['return_date']) for o in refreshed.values()})} date pair(s)  •  "
                 f"_Grid coverage:_ {date_combos_covered}/{total_combos} date pairs"
             ),
         }]},
    ]

    if threshold_alerts:
        lines = [f"🚨 *Below €{int(PRICE_THRESHOLD_EUR)} threshold* (newly crossed):"]
        for _, new, old in sorted(threshold_alerts, key=lambda x: x[1]["price"]):
            extra = f"  _(was €{int(old['price'])})_" if old else "  _(first sighting)_"
            lines.append(fmt_line(new) + extra)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    if drop_alerts:
        lines = [f"📉 *Price drops ≥ {DROP_PCT_THRESHOLD:.0f}% on refreshed combos:*"]
        for _, new, old, pct in sorted(drop_alerts, key=lambda x: -x[3]):
            lines.append(fmt_line(new) + f"  *-{pct:.1f}%* _(was €{int(old['price'])})_")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    if has_alerts and (is_summary_hour or is_first_run):
        blocks.append({"type": "divider"})

    # ---- daily summary block (uses all known prices in state) ----
    if is_summary_hour or is_first_run:
        title = "First run — current snapshot" if is_first_run else "Daily summary"

        sorted_all = sorted(state_prices.values(), key=lambda o: o["price"])[:TOP_N_SUMMARY]
        if sorted_all:
            lines = [f"*{title} — top {len(sorted_all)} cheapest known:*"]
            lines += [fmt_line(o) for o in sorted_all]
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

        by_dest: dict[str, dict] = {}
        for o in state_prices.values():
            d = o["destination"]
            if d not in by_dest or o["price"] < by_dest[d]["price"]:
                by_dest[d] = o
        if by_dest:
            lines = ["*Cheapest known per destination:*"]
            for dest in DESTINATIONS:
                if dest in by_dest:
                    o = by_dest[dest]
                    lines.append(
                        f"• *{dest}*: €{int(o['price'])} from *{o['origin']}* "
                        f"({o['outbound_date']} → {o['return_date']})"
                    )
                else:
                    lines.append(f"• *{dest}*: _no data yet_")
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

        if date_combos_covered < total_combos:
            blocks.append({"type": "context", "elements": [{
                "type": "mrkdwn",
                "text": f"_Full grid coverage reached after ~{(total_combos - date_combos_covered + CALLS_PER_RUN - 1) // CALLS_PER_RUN} more run(s)._"
            }]})

    return {"text": "Flight price check", "blocks": blocks}


# ---------------------------- Slack send ----------------------------
def post_to_slack(webhook_url: str, payload: dict) -> None:
    try:
        r = requests.post(webhook_url, json=payload, timeout=30)
        if r.status_code >= 300:
            log.error("Slack returned %s: %s", r.status_code, r.text[:300])
            sys.exit(1)
        log.info("Posted to Slack: %s", r.status_code)
    except requests.RequestException as e:
        log.error("Slack post failed: %s", e)
        sys.exit(1)


# ---------------------------- main ----------------------------
def main() -> int:
    api_key = os.environ.get("SERPAPI_KEY")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    dry_run = "--dry-run" in sys.argv

    if not api_key:
        log.error("SERPAPI_KEY env var missing")
        return 1
    if not webhook and not dry_run:
        log.error("SLACK_WEBHOOK_URL env var missing")
        return 1

    state = load_state()
    is_first_run = state.get("last_run") is None
    state_prices: dict[str, dict] = dict(state.get("prices") or {})
    state_prices_before: dict[str, dict] = dict(state_prices)
    rotation_index: int = int(state.get("rotation_index", 0) or 0)

    now_utc = datetime.now(timezone.utc)
    is_summary_hour = now_utc.hour in DAILY_SUMMARY_UTC_HOURS
    log.info("UTC hour=%d  summary_hour=%s  first_run=%s  rotation_idx=%d/%d",
             now_utc.hour, is_summary_hour, is_first_run, rotation_index, len(all_combos()))

    combos_this_run, next_rotation_index = get_combos_for_run(rotation_index)
    log.info("Will check %d combo(s) this run: %s", len(combos_this_run), combos_this_run)

    refreshed: dict[str, dict] = {}
    for i, (outbound, ret) in enumerate(combos_this_run):
        if i > 0:
            time.sleep(INTER_CALL_DELAY_SEC)
        log.info("Searching %s → %s", outbound, ret)
        offers = search_flights(api_key, outbound, ret)
        log.info("  → %d offers; %d distinct routes", len(offers), len({(o["origin"], o["destination"]) for o in offers}))

        # Replace any stale entries for THIS combo, then add fresh ones
        keys_to_drop = [k for k in state_prices if k.endswith(f"|{outbound}|{ret}")]
        for k in keys_to_drop:
            del state_prices[k]

        for k, v in reduce_to_cheapest(offers).items():
            v["checked_at"] = now_utc.isoformat()
            refreshed[k] = v
            state_prices[k] = v

    payload = build_message(
        refreshed, state_prices, state_prices_before,
        is_first_run=is_first_run, is_summary_hour=is_summary_hour,
    )

    if payload is None:
        log.info("Quiet run — no alerts and not summary hour. Skipping Slack.")
    else:
        if dry_run:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        elif webhook:
            post_to_slack(webhook, payload)

    save_state({
        "last_run": now_utc.isoformat(),
        "rotation_index": next_rotation_index,
        "prices": state_prices,
    })
    log.info("Done. Next rotation index will be %d.", next_rotation_index)
    return 0


if __name__ == "__main__":
    sys.exit(main())
