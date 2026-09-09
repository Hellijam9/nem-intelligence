"""
Script: Predispatch Price Tracker

Forward 30-min price forecasts by region, from the latest Predispatch run.
Alerts if any region's forecast price exceeds a threshold, as far out as
the current Predispatch run actually covers (~31h typically, sometimes
more/less) - not a fixed short window, since you want the earliest
possible warning even if the far-out forecast still has time to change.
Useful as corroborating context alongside spot_spike.py too (a predispatch
warning firing shortly before an actual spot spike is a stronger signal
than either alone).

Note: the price table here is keyed ('PDREGION', '') in AEMO's MMS export,
not "PREDISPATCHPRICE" as named in the original build doc - and PERIODID
is itself a full datetime string (half-hour steps), not an integer index.

Debounced by (region, period) pair so the same forecast period isn't
re-alerted every subsequent run just for existing at the same price - but
unlike a plain "first time only" debounce, it DOES re-alert if that
period's forecast price later moves >=10% (up or down) since last
reported, or drops back below the threshold entirely (episode over for
that period). Same threshold logic as cap_dayahead.py's change-gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

import nemweb_common as nw

PREDISPATCH_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Predispatch_Reports/"
PREDISPATCH_PATTERN = r"^PUBLIC_PREDISPATCH_\d{12}_\d{14}_LEGACY\.zip$"

NEM_TZ = timezone(timedelta(hours=10))
STATE_FILE = "predispatch_state.json"
ALERT_THRESHOLD = 300


def parse_period(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


def is_significant(prev: float, curr: float, threshold: float = 0.10) -> bool:
    if prev == 0 and curr == 0:
        return False
    if prev == 0 or curr == 0:
        return True
    return abs(curr - prev) / abs(prev) >= threshold


def pool_into_ranges(alerts: list[tuple]) -> list[dict]:
    """
    Groups alerts by (REGIONID, kind) into contiguous 30-min-period runs, so a sustained
    stretch above threshold reads as one "16:00 to 22:00" line instead of 12 separate
    per-period lines - per your request, that per-period listing was the actual noise
    source on a day with any real sustained price elevation, not the alert count itself.
    """
    by_key: dict[tuple, list[tuple]] = {}
    for row, kind, price, prev_price in alerts:
        by_key.setdefault((row["REGIONID"], kind), []).append((row["_period_dt"], price, prev_price))

    groups = []
    for (region, kind), entries in by_key.items():
        entries.sort(key=lambda e: e[0])
        run = [entries[0]]
        for entry in entries[1:]:
            if entry[0] - run[-1][0] == timedelta(minutes=30):
                run.append(entry)
            else:
                groups.append((region, kind, run))
                run = [entry]
        groups.append((region, kind, run))

    result = []
    for region, kind, run in groups:
        start = run[0][0]
        end = run[-1][0] + timedelta(minutes=30)
        prices = [e[1] for e in run]
        prev_prices = [e[2] for e in run if e[2] is not None]
        result.append({
            "region": region, "kind": kind, "start": start, "end": end,
            "price_min": min(prices), "price_max": max(prices),
            "prev_min": min(prev_prices) if prev_prices else None,
            "prev_max": max(prev_prices) if prev_prices else None,
        })
    return result


def main() -> None:
    cfg = nw.CONFIG
    threshold = cfg.get("predispatch_alert_threshold", ALERT_THRESHOLD)
    topic = cfg.get("ntfy_topics", {}).get("predispatch", "predispatch-alerts")

    try:
        files = nw.get_latest_files(PREDISPATCH_URL, PREDISPATCH_PATTERN, n=1)
    except Exception as exc:
        print(f"[predispatch_tracker] ERROR listing NEMWEB directory: {exc}")
        return

    df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(files[-1])), "PDREGION")
    df["RRP"] = pd.to_numeric(df["RRP"], errors="coerce")
    df["_period_dt"] = df["PERIODID"].apply(parse_period)

    now = datetime.now(NEM_TZ).replace(tzinfo=None)
    # No fixed horizon cap - looks as far out as this Predispatch run actually covers
    # (~31h typically, sometimes more/less depending on the run), not an arbitrary 2h window.
    window = df[df["_period_dt"] >= now]
    horizon_hours = (window["_period_dt"].max() - now).total_seconds() / 3600 if not window.empty else 0

    state = nw.read_state(STATE_FILE, default={"prices": {}})
    prev_prices = {tuple(k.split("|", 1)): v for k, v in state.get("prices", {}).items()}

    new_alerts = []
    dropped_alerts = []
    tracked_prices: dict[tuple, float] = {}

    for _, row in window.iterrows():
        key = (row["REGIONID"], row["PERIODID"])
        price = row["RRP"]
        prev_price = prev_prices.get(key)

        if price > threshold:
            tracked_prices[key] = price
            if prev_price is None:
                new_alerts.append((row, "new", price, None))
            elif is_significant(prev_price, price):
                new_alerts.append((row, "revised", price, prev_price))
        elif prev_price is not None:
            # Was previously above threshold, now back under it - episode over for this period.
            dropped_alerts.append((row, prev_price, price))

    nw.write_state(STATE_FILE, {"prices": {f"{k[0]}|{k[1]}": v for k, v in tracked_prices.items()}})

    if not new_alerts and not dropped_alerts:
        print(f"[predispatch_tracker] No new/changed forecast breaches of ${threshold} in the next {horizon_hours:.0f}h.")
        return

    lines = [f"Predispatch forecast: price(s) above ${threshold} within the next {horizon_hours:.0f}h:"]

    pooled_new = pool_into_ranges(new_alerts)
    pooled_dropped = pool_into_ranges([(row, "dropped", price, prev_price) for row, prev_price, price in dropped_alerts])

    for g in sorted(pooled_new, key=lambda g: (g["region"], g["start"])):
        span = f"{g['start'].strftime('%H:%M')} to {g['end'].strftime('%H:%M')}"
        price_str = f"${g['price_min']:,.0f}" if g["price_min"] == g["price_max"] else f"${g['price_min']:,.0f}-${g['price_max']:,.0f}"
        if g["kind"] == "new":
            lines.append(f"  {g['region']}: above ${threshold} from {span} NEM time ({price_str}/MWh)")
        else:
            prev_str = f"${g['prev_min']:,.0f}" if g['prev_min'] == g['prev_max'] else f"${g['prev_min']:,.0f}-${g['prev_max']:,.0f}"
            lines.append(f"  {g['region']}: revised to {price_str}/MWh (was {prev_str}) from {span} NEM time")

    for g in sorted(pooled_dropped, key=lambda g: (g["region"], g["start"])):
        span = f"{g['start'].strftime('%H:%M')} to {g['end'].strftime('%H:%M')}"
        price_str = f"${g['price_min']:,.0f}" if g["price_min"] == g["price_max"] else f"${g['price_min']:,.0f}-${g['price_max']:,.0f}"
        prev_str = f"${g['prev_min']:,.0f}" if g['prev_min'] == g['prev_max'] else f"${g['prev_min']:,.0f}-${g['prev_max']:,.0f}"
        lines.append(f"  {g['region']}: back under ${threshold} ({price_str}, was {prev_str}) from {span} NEM time")

    message = "\n".join(lines)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title=f"Predispatch: {len(new_alerts) + len(dropped_alerts)} forecast change(s)",
        message=message,
        tags=["crystal_ball"],
    )


if __name__ == "__main__":
    main()
