"""
PASA Monitor

Diffs the two most recent MTPASA DUID Availability snapshots and alerts on
any (DUID, DAY) combination whose declared availability moved by 100MW or
more - i.e. a generator has changed its own forward-looking declared
availability for a specific future day (planned outage added/removed/moved,
uprate, etc).

Source: MTPASA_DUIDAvailability (updates ~every 3 hours). Each snapshot is
one row per DUID per future day, out to roughly 3 years ahead.

Message format matches the old mtpasa_scheduler.py: one block per DUID
(name | owner | region, then full capacity/units, then one line per
grouped date range), rather than a flat list of windows. Grouping logic
itself (collapsing consecutive same-delta days into a window) matches
the old script's group_consecutive_changes - already equivalent before
this change, since both scripts solve the same "44 day-rows -> a few
readable events" problem the same way.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pandas as pd
from dateutil.relativedelta import relativedelta

import nemweb_common as nw

PASA_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/MTPASA_DUIDAvailability/"
PASA_PATTERN = r"^PUBLIC_MTPASADUIDAVAILABILITY_\d{12}_\d{16}\.zip$"

THRESHOLD_MW = 100
STATE_FILE = "pasa_last_processed.json"


def quarter_label(dt: datetime) -> str:
    q = (dt.month - 1) // 3 + 1
    return f"Q{q} {dt.year}"


def duration_label(from_dt: datetime, to_dt: datetime) -> str:
    """Human-readable relativedelta duration, e.g. '5 days', '2 months, 3 days'."""
    if to_dt < from_dt:
        return "in the past"
    rd = relativedelta(to_dt, from_dt)
    parts = []
    if rd.years:
        parts.append(f"{rd.years}y")
    if rd.months:
        parts.append(f"{rd.months}mo")
    if rd.days:
        parts.append(f"{rd.days}d")
    return " ".join(parts) if parts else "today"


def parse_day(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


RECENT_WINDOWS_FILE = "pasa_recent_windows.json"
RECENT_WINDOWS_KEEP_DAYS = 120  # keep windows whose end date is up to this far in the past, for market_read.py


def update_recent_windows_cache(windows: list[dict], reg_lookup) -> None:
    """
    Persist newly-found outage windows into a rolling cache, so market_read.py
    (the cause-and-effect synthesis layer) can see "what outages are known
    right now" without re-downloading and re-diffing the ~250k-row PASA
    snapshots itself on every run.
    """
    cache = nw.read_state(RECENT_WINDOWS_FILE, default=[])
    now = datetime.now()

    # Dedupe key: the same real declared change can otherwise get appended again on a later
    # run (e.g. a slightly-revised re-declaration across consecutive ~3-hourly snapshots still
    # nets out to the same window) - found as a real bug (16 windows duplicated 2-3x each in
    # the live cache) while checking market_read.py's output.
    existing_keys = {(c["duid"], c["region"], c["delta"], c["start"], c["end"]) for c in cache}

    for w in windows:
        key = (w["duid"], w["region"], w["delta"], w["start"].strftime("%Y-%m-%d"), w["end"].strftime("%Y-%m-%d"))
        if key in existing_keys:
            continue
        existing_keys.add(key)
        station = owner = ""
        if reg_lookup is not None and w["duid"] in reg_lookup.index:
            reg_row = reg_lookup.loc[w["duid"]]
            station = reg_row.get("STATIONNAME") or reg_row.get("UNIT_NAME") or ""
            owner = reg_row.get("Owner") or ""
        cache.append({
            "duid": w["duid"],
            "station": station,
            "owner": owner,
            "region": w["region"],
            "delta": w["delta"],
            "start": w["start"].strftime("%Y-%m-%d"),
            "end": w["end"].strftime("%Y-%m-%d"),
            "found_at": now.isoformat(),
        })

    # prune windows that ended too long ago to still be relevant
    cutoff = now - timedelta(days=RECENT_WINDOWS_KEEP_DAYS)
    cache = [c for c in cache if datetime.strptime(c["end"], "%Y-%m-%d") >= cutoff]
    nw.write_state(RECENT_WINDOWS_FILE, cache)


def group_into_windows(changes: pd.DataFrame) -> list[dict]:
    """
    A single declared outage/uprate shows up as the *same* delta repeated
    across every consecutive day it covers (e.g. a 2-week planned outage is
    ~14 identical-delta rows, one per DAY). Collapse those into one
    "outage window" per (DUID, delta) run of consecutive days, rather than
    alerting once per day - the difference between a readable alert and a
    44-line wall of text for what is really 3-4 distinct events.
    """
    windows = []
    changes = changes.copy()
    changes["_day_dt"] = changes["DAY"].apply(parse_day)
    changes["_delta_rounded"] = changes["delta"].round().astype(int)

    for (duid, delta_rounded), group in changes.groupby(["DUID", "_delta_rounded"]):
        group = group.sort_values("_day_dt")
        run_start = None
        run_end = None
        run_row = None
        for _, row in group.iterrows():
            if run_start is None:
                run_start = run_end = row["_day_dt"]
                run_row = row
                continue
            if (row["_day_dt"] - run_end).days == 1:
                run_end = row["_day_dt"]
            else:
                windows.append({"duid": duid, "delta": delta_rounded, "region": run_row["REGIONID"],
                                 "start": run_start, "end": run_end})
                run_start = run_end = row["_day_dt"]
                run_row = row
        if run_start is not None:
            windows.append({"duid": duid, "delta": delta_rounded, "region": run_row["REGIONID"],
                             "start": run_start, "end": run_end})
    return windows


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("pasa", "pasa-alerts")

    try:
        files = nw.get_latest_files(PASA_URL, PASA_PATTERN, n=2)
    except Exception as exc:
        print(f"[pasa_monitor] ERROR listing NEMWEB directory: {exc}")
        sys.exit(1)

    if len(files) < 2:
        print("[pasa_monitor] Not enough snapshots yet to diff.")
        return

    previous_url, latest_url = files[0], files[1]

    last_processed = nw.read_state(STATE_FILE, default={}).get("latest_url")
    if last_processed == latest_url:
        print("[pasa_monitor] Already processed this snapshot pair - no new data since last run.")
        return

    print(f"[pasa_monitor] Comparing:\n  previous: {previous_url}\n  latest:   {latest_url}")

    prev_df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(previous_url)), "MTPASA_DUIDAVAILABILITY")
    curr_df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(latest_url)), "MTPASA_DUIDAVAILABILITY")

    for df in (prev_df, curr_df):
        df["PASAAVAILABILITY"] = pd.to_numeric(df["PASAAVAILABILITY"], errors="coerce")

    merged = prev_df[["DUID", "DAY", "REGIONID", "PASAAVAILABILITY"]].merge(
        curr_df[["DUID", "DAY", "PASAAVAILABILITY"]],
        on=["DUID", "DAY"],
        how="outer",
        suffixes=("_prev", "_curr"),
    )
    merged["PASAAVAILABILITY_prev"] = merged["PASAAVAILABILITY_prev"].fillna(0)
    merged["PASAAVAILABILITY_curr"] = merged["PASAAVAILABILITY_curr"].fillna(0)
    merged["delta"] = merged["PASAAVAILABILITY_curr"] - merged["PASAAVAILABILITY_prev"]

    changes = merged[merged["delta"].abs() >= THRESHOLD_MW].copy()

    nw.write_state(STATE_FILE, {"latest_url": latest_url, "checked_at": datetime.now().isoformat()})

    if changes.empty:
        print(f"[pasa_monitor] No changes >= {THRESHOLD_MW}MW between the last two snapshots.")
        return

    registry = nw.load_registry()
    reg_lookup = registry.merged.set_index("DUID") if registry.merged is not None else None

    windows = group_into_windows(changes)
    windows.sort(key=lambda w: abs(w["delta"]), reverse=True)
    update_recent_windows_cache(windows, reg_lookup)

    now = datetime.now()

    # Group windows by DUID for display, same shape as the old mtpasa_scheduler.py
    # output (one block per DUID, not one line per window) - biggest change first.
    duid_windows: dict[str, list[dict]] = {}
    for w in windows:
        duid_windows.setdefault(w["duid"], []).append(w)
    ordered_duids = sorted(duid_windows, key=lambda d: max(abs(w["delta"]) for w in duid_windows[d]), reverse=True)

    lines = [f"Changes in Availability by DUID (>= {THRESHOLD_MW} MW):"]
    for duid in ordered_duids:
        station = owner = ""
        capacity = units = "UNKNOWN"
        ws = duid_windows[duid]
        region = ws[0]["region"]
        if reg_lookup is not None and duid in reg_lookup.index:
            reg_row = reg_lookup.loc[duid]
            station = reg_row.get("STATIONNAME") or reg_row.get("UNIT_NAME") or ""
            owner = reg_row.get("Owner") or "UNKNOWN"
            capacity = reg_row.get("Nameplate Capacity (MW)", "UNKNOWN")
            units = reg_row.get("Number of Units", "UNKNOWN")
        name = station or duid

        lines.append("")
        lines.append(f"- {duid} | {name} | {owner or 'UNKNOWN'} | {region}")
        lines.append(f"    Full capacity: {capacity} MW | Units: {units}")
        for w in ws:
            sign = "+" if w["delta"] > 0 else ""
            span = w["start"].strftime("%Y-%m-%d") if w["start"] == w["end"] else \
                f"{w['start'].strftime('%Y-%m-%d')} to {w['end'].strftime('%Y-%m-%d')}"
            lines.append(f"    {span} ({duration_label(now, w['start'])}, {quarter_label(w['start'])}): {sign}{w['delta']:.0f} MW")

    message = "\n".join(lines)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title=f"PASA: {len(windows)} availability change window(s) >= {THRESHOLD_MW}MW",
        message=message,
        tags=["chart_with_upwards_trend"],
    )


if __name__ == "__main__":
    main()
