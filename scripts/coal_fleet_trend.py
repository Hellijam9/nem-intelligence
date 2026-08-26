"""
Script: Coal Fleet Aggregate Trend (weekly rollup)

Per the QED historical remap: single-event outages (Callide, Yallourn) are
already caught by pasa_monitor.py and scada_drop_monitor.py, but the bigger
multi-quarter price driver since 2023 has been the slow *structural*
decline in coal fleet availability itself - a trend, not an event, which
needs a different script than event-alerting.

This computes the current 7-day-forward average declared coal availability
(MW) from the same MTPASA_DUIDAvailability snapshot pasa_monitor.py already
uses, and appends it to a local running history file. Once that history
has accumulated a few weeks/months of runs, this starts reporting genuine
week-over-week and month-over-month trend deltas - it can't manufacture a
"vs last year" comparison on day one, so it only reports comparisons for
which it actually has a prior data point, rather than a fabricated one.

(A same-day-last-year comparator could in principle be pulled from AEMO's
MMSDM historical archive tier instead of waiting a year - not built here,
flagged as a possible future enhancement since it needs checking whether
that archive actually carries MTPASA tables before relying on it.)

Needs the fuel registry (Scheduled Plant Information Fuel CSV) to restrict
to coal specifically; without it, falls back to all-DUID aggregate
availability with a loud caveat, same pattern as scada_drop_monitor.py.

Only pushes to ntfy if this week's figure differs (rounded to 1 decimal
MW) from last week's recorded figure - per your request, no repeat
notification if the trend hasn't actually moved.

Comparisons (week/month/year-ago, and the "last recorded" check above)
only ever match against a history entry recorded with the same
fuel_filtered setting as the current run - found and fixed a real bug
where an old unfiltered all-fleet entry (44,814MW) was being compared
against a correctly coal-filtered one (18,703MW), producing a fake
"-58.3%" swing that was really just a filtering mismatch, not a real
coal-fleet trend.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import pandas as pd

import nemweb_common as nw

PASA_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/MTPASA_DUIDAvailability/"
PASA_PATTERN = r"^PUBLIC_MTPASADUIDAVAILABILITY_\d{12}_\d{16}\.zip$"

NEM_TZ = timezone(timedelta(hours=10))
HISTORY_FILE = nw.STATE_DIR / "coal_fleet_history.csv"
FORWARD_WINDOW_DAYS = 7
COAL_FUELS = {"black coal", "brown coal"}


def fuel_column(fuel_info: pd.DataFrame) -> str | None:
    for candidate in ("FUEL", "Fuel", "FUELTYPE", "FUEL_TYPE"):
        if candidate in fuel_info.columns:
            return candidate
    return None


def read_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, newline="") as f:
        return list(csv.DictReader(f))


def append_history(row: dict) -> None:
    file_exists = HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "avg_available_mw", "fuel_filtered"])
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def closest_entry(history: list[dict], target_date: datetime, fuel_filtered: bool, tolerance_days: int = 3) -> dict | None:
    """Only considers rows recorded with the same fuel_filtered setting as the current run -
    otherwise a coal-only figure could get compared against an old all-fleet (unfiltered)
    entry, producing a fake trend swing that's really just a filtering mismatch."""
    best, best_diff = None, None
    for row in history:
        if row.get("fuel_filtered") != str(fuel_filtered):
            continue
        row_date = datetime.strptime(row["date"], "%Y-%m-%d")
        diff = abs((row_date - target_date).days)
        if diff <= tolerance_days and (best_diff is None or diff < best_diff):
            best, best_diff = row, diff
    return best


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("pasa", "pasa-alerts")  # shares the PASA topic - same audience/purpose

    files = nw.get_latest_files(PASA_URL, PASA_PATTERN, n=1)
    df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(files[-1])), "MTPASA_DUIDAVAILABILITY")
    df["PASAAVAILABILITY"] = pd.to_numeric(df["PASAAVAILABILITY"], errors="coerce")
    df["_day_dt"] = df["DAY"].apply(lambda s: datetime.strptime(s.strip(), "%Y/%m/%d %H:%M:%S"))

    registry = nw.load_registry()
    fuel_filtered = False
    if registry.fuel_info is not None:
        fcol = fuel_column(registry.fuel_info)
        if fcol:
            coal_duids = set(registry.fuel_info.loc[
                registry.fuel_info[fcol].str.lower().isin(COAL_FUELS), "DUID"
            ])
            df = df[df["DUID"].isin(coal_duids)]
            fuel_filtered = True

    if not fuel_filtered:
        print("[coal_fleet_trend] NOTE: no fuel registry supplied - reporting ALL-DUID aggregate "
              "availability, not coal-specific. Supply the fuel CSV for a true coal-only figure.")

    now = datetime.now(NEM_TZ).replace(tzinfo=None)
    window_end = now + timedelta(days=FORWARD_WINDOW_DAYS)
    window = df[(df["_day_dt"] >= now) & (df["_day_dt"] <= window_end)]

    if window.empty:
        print("[coal_fleet_trend] No data in the forward window - unexpected, check the snapshot.")
        return

    # average total declared availability across the 7 forward days
    daily_totals = window.groupby(window["_day_dt"].dt.date)["PASAAVAILABILITY"].sum()
    avg_available_mw = daily_totals.mean()

    today_str = now.strftime("%Y-%m-%d")
    history = read_history()
    same_filter_history = [r for r in history if r.get("fuel_filtered") == str(fuel_filtered)]
    last_recorded = float(same_filter_history[-1]["avg_available_mw"]) if same_filter_history else None
    append_history({"date": today_str, "avg_available_mw": f"{avg_available_mw:.1f}", "fuel_filtered": fuel_filtered})

    label = "Coal" if fuel_filtered else "All-fleet (unfiltered)"
    lines = [f"{label} aggregate declared availability, {FORWARD_WINDOW_DAYS}-day forward avg: {avg_available_mw:,.0f}MW"]

    for period_name, days_ago in (("week-ago", 7), ("month-ago", 30), ("year-ago", 365)):
        ref = closest_entry(history, now - timedelta(days=days_ago), fuel_filtered)
        if ref is None:
            lines.append(f"  vs {period_name}: not enough history yet")
            continue
        ref_val = float(ref["avg_available_mw"])
        delta = avg_available_mw - ref_val
        pct = (delta / ref_val * 100) if ref_val else 0
        lines.append(f"  vs {period_name} ({ref['date']}): {delta:+,.0f}MW ({pct:+.1f}%)")

    message = "\n".join(lines)
    print(message)

    # Only notify if this week's figure actually differs from last week's recorded one -
    # the history file always gets a new row every run either way, this just gates the push.
    if last_recorded is not None and round(avg_available_mw, 1) == round(last_recorded, 1):
        print("[coal_fleet_trend] No change since last recorded figure - not pushing.")
        return

    nw.push_ntfy(
        topic=topic,
        title=f"{label} availability trend",
        message=message,
        tags=["chart_with_downwards_trend" if fuel_filtered else "bar_chart"],
    )


if __name__ == "__main__":
    main()
