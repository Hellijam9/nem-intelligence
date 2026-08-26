"""
Script: Negative Pricing Tracker (weekly rollup)

Per the QED historical remap: negative-price frequency climbed almost
monotonically across 8 years of QED reports (3.6% of intervals in Q2 2020
-> an all-time 31.0% in Q4 2025) and is the physical mechanism behind the
"battery growth is eroding cap value" thesis in the build doc's own
forward-view notes. This tracks that frequency on a rolling weekly basis
so the thesis can be checked against real data rather than held only
qualitatively until the next QED report.

Source: Public_Prices (same settled daily feed cap_quarter_to_date.py
accumulates) for the last 7 published days, aggregated into one weekly
data point per run, appended to a local history file the same way
coal_fleet_trend.py does.

Scope note: this tracks negative *pricing* frequency, which is well-defined
from DISPATCHPRICE/settled prices alone. Actual curtailment (available
wind/solar output that went undispatched) is a related but separate
metric requiring a comparison between SCADA output and each semi-scheduled
DUID's forecast availability - not built here; flagged as a natural
follow-on script once that data source is mapped, rather than shipping an
approximate/unreliable curtailment number now.

Only pushes to ntfy if any region's percentage actually changed (rounded
to 1 decimal) from the last recorded figure - per your request, no repeat
notification if the trend hasn't moved.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import pandas as pd

import nemweb_common as nw

PUBLIC_PRICES_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Public_Prices/"

NEM_TZ = timezone(timedelta(hours=10))
HISTORY_FILE = nw.STATE_DIR / "negative_pricing_history.csv"
DAYS_TO_AGGREGATE = 7


def read_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, newline="") as f:
        return list(csv.DictReader(f))


def append_history(row: dict, regions: list[str]) -> None:
    file_exists = HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["week_ending"] + [f"{r}_pct" for r in regions])
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def closest_entry(history: list[dict], target_date: datetime, tolerance_days: int = 5) -> dict | None:
    best, best_diff = None, None
    for row in history:
        row_date = datetime.strptime(row["week_ending"], "%Y-%m-%d")
        diff = abs((row_date - target_date).days)
        if diff <= tolerance_days and (best_diff is None or diff < best_diff):
            best, best_diff = row, diff
    return best


def main() -> None:
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    topic = cfg.get("ntfy_topics", {}).get("pasa", "pasa-alerts")  # structural/weekly, shares the low-frequency topic

    now = datetime.now(NEM_TZ).replace(tzinfo=None)

    all_rows = []
    for days_ago in range(1, DAYS_TO_AGGREGATE + 1):
        day = now - timedelta(days=days_ago)
        compact = day.strftime("%Y%m%d")
        try:
            files = nw.list_nemweb_files(PUBLIC_PRICES_URL, rf"^PUBLIC_PRICES_{compact}0000_\d+\.zip$")
        except Exception as exc:
            print(f"[negative_pricing_tracker] WARNING: could not list {compact}: {exc}")
            continue
        if not files:
            continue
        try:
            df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(files[-1])), "DREGION")
        except Exception as exc:
            print(f"[negative_pricing_tracker] WARNING: could not parse {compact}: {exc}")
            continue
        df["RRP"] = pd.to_numeric(df["RRP"], errors="coerce")
        # AEMO's Public_Prices DREGION table publishes every interval twice (confirmed while
        # fixing cap_yesterday.py/cap_quarter_to_date.py) - doesn't change this script's
        # percentage (a ratio, unaffected by exact duplication) but dedupe anyway for hygiene.
        df = df.drop_duplicates(subset=["SETTLEMENTDATE", "REGIONID"])
        all_rows.append(df)

    if not all_rows:
        print("[negative_pricing_tracker] No Public_Prices data available for the lookback window.")
        return

    combined = pd.concat(all_rows, ignore_index=True)

    week_ending = now.strftime("%Y-%m-%d")
    row = {"week_ending": week_ending}
    lines = [f"Negative/zero pricing, trailing {DAYS_TO_AGGREGATE} days (week ending {week_ending}):"]

    history = read_history()
    last_recorded = history[-1] if history else None

    changed = last_recorded is None
    for region in regions:
        region_df = combined[combined["REGIONID"] == region]
        if region_df.empty:
            continue
        pct_negative = (region_df["RRP"] <= 0).mean() * 100
        row[f"{region}_pct"] = f"{pct_negative:.2f}"

        if last_recorded is not None:
            prior = last_recorded.get(f"{region}_pct")
            if prior is None or round(pct_negative, 1) != round(float(prior), 1):
                changed = True

        trend_bits = []
        for period_name, days_ago in (("month-ago", 30), ("year-ago", 365)):
            ref = closest_entry(history, now - timedelta(days=days_ago))
            if ref and f"{region}_pct" in ref and ref[f"{region}_pct"]:
                ref_val = float(ref[f"{region}_pct"])
                trend_bits.append(f"{period_name} {ref_val:.1f}% ({pct_negative - ref_val:+.1f}pp)")
        trend_str = f"  [{', '.join(trend_bits)}]" if trend_bits else "  [not enough history yet]"
        lines.append(f"  {region}: {pct_negative:.1f}% of intervals{trend_str}")

    append_history(row, regions)

    message = "\n".join(lines)
    print(message)

    # Only notify if any region's percentage actually changed since the last recorded figure -
    # the history file still gets a new row every run either way, this just gates the push.
    if not changed:
        print("[negative_pricing_tracker] No change since last recorded figure - not pushing.")
        return

    nw.push_ntfy(
        topic=topic,
        title="Negative pricing trend (weekly)",
        message=message,
        tags=["chart_with_downwards_trend"],
    )


if __name__ == "__main__":
    main()
