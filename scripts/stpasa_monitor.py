"""
STPASA Monitor

The short-term counterpart of pasa_monitor.py. Diffs the latest STPASA DUID Availability
snapshot against the last one already processed and alerts on any (DUID, half-hour) whose
declared generation availability moved by THRESHOLD_MW or more - i.e. a unit has changed its
own declared availability for the next ~7 days (outage added/removed/moved, derating, uprate).

Why this exists: MTPASA_DUIDAvailability (pasa_monitor.py) updates ~every 3 hours and lags -
a near-term change (e.g. Bayswater BW04 -685MW from 13 Nov, 2026-11-11) showed up in the hourly
STPASA file a few hours before MTPASA, and nothing alerted on STPASA unit changes.

Source: STPASA_DUIDAvailability, one file per hour, one row per DUID per half-hour out to
roughly 7 days ahead. Same size limit as MTPASA (100MW), same ntfy topic, same message layout
(one block per DUID, then one line per window) so the two read the same on the phone.

Diffs against the last PROCESSED snapshot (not just the file before the latest) so a missed
run can't silently swallow the changes in the files it skipped; falls back to the file
before the latest when the last processed one has already rolled off the directory.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta

import pandas as pd
from dateutil.relativedelta import relativedelta

import nemweb_common as nw
import pasa_monitor as pm

STPASA_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/STPASA_DUIDAvailability/"
STPASA_PATTERN = r"^PUBLIC_STPASA_DUIDAVAILABILITY_\d{12}_\d+\.zip$"

THRESHOLD_MW = 100  # same limit as pasa_monitor.py
STATE_FILE = "stpasa_last_processed.json"
INTERVAL = timedelta(minutes=30)
# Windows at least this long also go into pasa_recent_windows.json (day-level, shared with
# pasa_monitor.py) so the morning/Monday recaps' "Units out today" / "Large outages" see them
# straight away rather than waiting for MTPASA to catch up. Shorter intraday dips would read
# as a whole-day outage there, so they stay alert-only.
CACHE_MIN_HOURS = 12
EPISODE_GAP = timedelta(hours=6)  # windows closer than this merge into one episode in the alert
MAX_UNITS_SHOWN = 12
AVAIL_COL = "GENERATION_PASA_AVAILABILITY"


def parse_interval(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


def duration_label(from_dt: datetime, to_dt: datetime) -> str:
    if to_dt <= from_dt:
        return "now"
    rd = relativedelta(to_dt, from_dt)
    parts = []
    if rd.months:
        parts.append(f"{rd.months}mo")
    if rd.days:
        parts.append(f"{rd.days}d")
    if rd.hours:
        parts.append(f"{rd.hours}h")
    if not parts:
        parts.append(f"{rd.minutes}m")
    return " ".join(parts)


def group_into_windows(changes: pd.DataFrame) -> list[dict]:
    """
    A single declared outage shows up as the same delta repeated across every consecutive
    half-hour it covers (a 5-day outage is 240 identical rows). Collapse each run of
    consecutive intervals with the same (DUID, rounded delta) into one window.
    """
    windows = []
    changes = changes.copy()
    changes["_dt"] = changes["INTERVAL_DATETIME"].apply(parse_interval)
    changes["_delta"] = changes["delta"].round().astype(int)

    for (duid, delta), group in changes.groupby(["DUID", "_delta"]):
        group = group.sort_values("_dt")
        start = end = None
        for dt in group["_dt"]:
            if start is None:
                start = end = dt
            elif dt - end == INTERVAL:
                end = dt
            else:
                windows.append({"duid": duid, "delta": delta, "start": start, "end": end})
                start = end = dt
        if start is not None:
            windows.append({"duid": duid, "delta": delta, "start": start, "end": end})
    return windows


def summarise_episodes(ws: list[dict]) -> list[dict]:
    """
    Collapse a unit's raw windows into episodes: windows that touch or sit within EPISODE_GAP
    of each other are one episode (a unit sagging by a slightly different amount every
    half-hour over a day is one event, not 40). Each episode reports its span and the end
    result - the worst fall (down) and best rise (up) in it - not every step.
    """
    eps = []
    for w in sorted(ws, key=lambda x: x["start"]):
        end = w["end"] + INTERVAL
        if eps and w["start"] <= eps[-1]["end"] + EPISODE_GAP:
            ep = eps[-1]
            ep["end"] = max(ep["end"], end)
            ep["deltas"].append(w["delta"])
        else:
            eps.append({"start": w["start"], "end": end, "deltas": [w["delta"]]})
    out = []
    for ep in eps:
        downs = [d for d in ep["deltas"] if d < 0]
        ups = [d for d in ep["deltas"] if d > 0]
        out.append({
            "start": ep["start"], "end": ep["end"],
            "down": min(downs) if downs else 0, "up": max(ups) if ups else 0,
            "varies": len({round(d, -1) for d in ep["deltas"]}) > 1,
        })
    return out


def snapshot_stamp(url: str) -> str:
    m = re.search(r"_(\d{12})_", url)
    return m.group(1) if m else url


def load_snapshot(url: str) -> pd.DataFrame:
    df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(url)), "STPASA_DUIDAVAILABILITY")
    df[AVAIL_COL] = pd.to_numeric(df[AVAIL_COL], errors="coerce")
    return df[["DUID", "INTERVAL_DATETIME", AVAIL_COL]]


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("pasa", "pasa-alerts")

    try:
        files = nw.get_latest_files(STPASA_URL, STPASA_PATTERN, n=72)
    except Exception as exc:
        print(f"[stpasa_monitor] ERROR listing NEMWEB directory: {exc}")
        sys.exit(1)

    if len(files) < 2:
        print("[stpasa_monitor] Not enough snapshots yet to diff.")
        return

    latest_url = files[-1]
    last_processed = nw.read_state(STATE_FILE, default={}).get("latest_url")
    if last_processed == latest_url:
        print("[stpasa_monitor] Already processed this snapshot - no new data since last run.")
        return

    previous_url = last_processed if last_processed in files else files[-2]
    print(f"[stpasa_monitor] Comparing:\n  previous: {previous_url}\n  latest:   {latest_url}")

    prev_df = load_snapshot(previous_url)
    curr_df = load_snapshot(latest_url)

    # Inner join on (DUID, interval): the ~7 day horizon rolls forward every hour, so
    # intervals enter at the far edge and drop off the near edge. An outer join would read
    # those as fake 0 <-> full-capacity jumps (same trap pasa_monitor.py documents).
    merged = prev_df.merge(curr_df, on=["DUID", "INTERVAL_DATETIME"], how="inner", suffixes=("_prev", "_curr"))
    merged["delta"] = merged[f"{AVAIL_COL}_curr"] - merged[f"{AVAIL_COL}_prev"]
    changes = merged[merged["delta"].abs() >= THRESHOLD_MW].copy()

    nw.write_state(STATE_FILE, {"latest_url": latest_url, "checked_at": datetime.now(nw.NEM_TZ).isoformat()})

    if changes.empty:
        print(f"[stpasa_monitor] No changes >= {THRESHOLD_MW}MW since the last snapshot.")
        return

    registry = nw.load_registry()
    reg_lookup = registry.merged.set_index("DUID") if registry.merged is not None else None

    windows = group_into_windows(changes)
    windows.sort(key=lambda w: (-abs(w["delta"]), w["start"]))
    now = datetime.now(nw.NEM_TZ).replace(tzinfo=None)

    cache_windows = []
    for w in windows:
        end = w["end"] + INTERVAL
        if end - w["start"] < timedelta(hours=CACHE_MIN_HOURS):
            continue
        region = ""
        if reg_lookup is not None and w["duid"] in reg_lookup.index:
            region = reg_lookup.loc[w["duid"]].get("REGIONID") or ""
        cache_windows.append({"duid": w["duid"], "delta": w["delta"], "region": region,
                              "start": w["start"], "end": end - timedelta(seconds=1)})
    if cache_windows:
        pm.update_recent_windows_cache(cache_windows, reg_lookup)

    duid_windows: dict[str, list[dict]] = {}
    for w in windows:
        duid_windows.setdefault(w["duid"], []).append(w)
    ordered = sorted(duid_windows, key=lambda d: max(abs(w["delta"]) for w in duid_windows[d]), reverse=True)

    # Summary, not a change log: one entry per unit and episode (see summarise_episodes), only
    # the biggest MAX_UNITS_SHOWN units listed, the rest counted.
    lines = [f"Short-term PASA: {len(ordered)} unit(s) with availability changes >= {THRESHOLD_MW} MW "
             f"(snapshot {snapshot_stamp(previous_url)} -> {snapshot_stamp(latest_url)}):"]
    for duid in ordered[:MAX_UNITS_SHOWN]:
        station = owner = ""
        capacity = None
        region = ""
        if reg_lookup is not None and duid in reg_lookup.index:
            reg_row = reg_lookup.loc[duid]
            station = reg_row.get("STATIONNAME") or reg_row.get("UNIT_NAME") or ""
            owner = reg_row.get("Owner") or ""
            region = reg_row.get("Region") or reg_row.get("REGIONID") or ""
            capacity = reg_row.get("Nameplate Capacity (MW)")
        try:
            cap_txt = f" ({float(capacity):,.0f} MW unit)" if capacity is not None and 0 < float(capacity) <= 3000 else ""
        except (TypeError, ValueError):
            cap_txt = ""
        lines.append(f"- {duid} | {station or duid} | {owner or 'UNKNOWN'}" + (f" | {region}" if region else "") + cap_txt)
        for ep in summarise_episodes(duid_windows[duid]):
            parts = []
            if ep["down"]:
                parts.append(f"down {abs(ep['down']):.0f} MW" + (" at worst" if ep["varies"] else ""))
            if ep["up"]:
                parts.append(f"up {ep['up']:.0f} MW" + (" at best" if ep["varies"] else ""))
            span = f"{ep['start']:%a %d %b %H:%M} to {ep['end']:%a %d %b %H:%M}"
            lines.append(f"    {span} (starts in {duration_label(now, ep['start'])}): {' / '.join(parts)}")
    if len(ordered) > MAX_UNITS_SHOWN:
        lines.append(f"+ {len(ordered) - MAX_UNITS_SHOWN} more unit(s) with smaller changes.")

    message = "\n".join(lines)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title=f"STPASA: {len(ordered)} unit(s) changed >= {THRESHOLD_MW}MW",
        message=message,
        tags=["zap"],
    )


if __name__ == "__main__":
    main()
