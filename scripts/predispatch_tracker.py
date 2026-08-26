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
    for row, kind, price, prev_price in sorted(new_alerts, key=lambda a: a[0]["_period_dt"]):
        time_str = row["_period_dt"].strftime("%H:%M")
        if kind == "new":
            lines.append(f"  {row['REGIONID']}: ${price:,.0f}/MWh forecast for {time_str} NEM time")
        else:
            lines.append(f"  {row['REGIONID']}: revised to ${price:,.0f}/MWh (was ${prev_price:,.0f}) for {time_str} NEM time")
    for row, prev_price, price in sorted(dropped_alerts, key=lambda a: a[0]["_period_dt"]):
        time_str = row["_period_dt"].strftime("%H:%M")
        lines.append(f"  {row['REGIONID']}: back under ${threshold} (${price:,.0f}, was ${prev_price:,.0f}) for {time_str} NEM time")

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
