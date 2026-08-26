"""
Script: Interconnector Flow Monitor

Watches DISPATCHINTERCONNECTORRES (same DispatchIS zip as cap_intraday.py /
spot_spike.py) and alerts when a flow gets close to its limit in whichever
direction it's currently running.

Confirmed live interconnector IDs (2026-07-28 snapshot) - not the IDs named
in the original build doc, which used shorthand labels rather than the
actual NEMWEB IDs:
    NSW1-QLD1   QNI (the main QLD-NSW AC interconnector)
    N-Q-MNSP1   Terranora (QNI's smaller DC sibling, QLD-NSW)
    VIC1-NSW1   VIC-NSW
    V-SA        Heywood (VIC-SA)
    V-S-MNSP1   Murraylink (VIC-SA, the *second* VIC-SA interconnector -
                 missing from the original build doc's "key interconnectors"
                 list, despite being named in the user's own Q1 2026 QED
                 notes as binding alongside Heywood during the Jan-2026 SA
                 heat event)
    T-V-MNSP1   Basslink (TAS-VIC)

Per the QED historical remap, Heywood and Murraylink are the highest-value
pair to watch (most repeated cause of SA price divergence), so both get a
"priority" tag on their alerts - but all six are monitored.

Utilization uses whichever of EXPORTLIMIT/IMPORTLIMIT matches the current
flow direction, since AEMO's sign convention for export/import isn't
uniform across every interconnector ID (some ID's "positive" direction is
defined the other way round) - abs(flow)/abs(matching limit) is the robust
form regardless of which way a given ID's convention runs.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

import nemweb_common as nw

DISPATCHIS_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
DISPATCHIS_PATTERN = r"^PUBLIC_DISPATCHIS_\d{12}_\d{16}\.zip$"

NEM_TZ = timezone(timedelta(hours=10))
STATE_FILE = "interconnector_state.json"
LOOKBACK_FILES = 6  # this one only needs the current state, not history - a small buffer covers a missed run

INTERCONNECTOR_LABELS = {
    "NSW1-QLD1": "QNI (QLD-NSW)",
    "N-Q-MNSP1": "Terranora (QNI minor, QLD-NSW)",
    "VIC1-NSW1": "VIC-NSW",
    "V-SA": "Heywood (VIC-SA)",
    "V-S-MNSP1": "Murraylink (VIC-SA)",
    "T-V-MNSP1": "Basslink (TAS-VIC)",
}
PRIORITY_INTERCONNECTORS = {"V-SA", "V-S-MNSP1"}  # Heywood + Murraylink - see module docstring

ENTER_THRESHOLD = 0.90  # alert once utilization reaches this
EXIT_THRESHOLD = 0.80   # must drop back below this before it can alert again (hysteresis, avoids flapping at 90%)


def parse_settlementdate(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


def utilization(flow: float, export_limit: float, import_limit: float) -> float | None:
    limit = export_limit if flow >= 0 else import_limit
    if limit == 0:
        return None
    return abs(flow) / abs(limit)


HIO_STATE_FILE = "interconnector_hio_state.json"
HIO_HORIZON_DAYS = 60  # this script's own real-time cadence only alerts on near-term changes -
# the full longer-range (weeks to years ahead) view lives in market_read.py's digest instead


def check_high_impact_outages() -> list[str]:
    """
    Checks AEMO's High Impact Outages feed (planned transmission/interconnector outages, NOT
    generators) for anything NEW or CHANGED affecting one of the 6 tracked interconnectors in
    the next HIO_HORIZON_DAYS - not something this script tracked before you asked whether
    future interconnector outages were accounted for anywhere. This feed only refreshes
    weekly, so debounced against a persisted state file the same way every other alert here
    is - otherwise this would re-alert the same unchanged outage every 5 minutes.
    """
    try:
        df = nw.fetch_high_impact_outages()
    except Exception as exc:
        print(f"[interconnector_monitor] WARNING: could not fetch High Impact Outages: {exc}")
        return []

    now = datetime.now(NEM_TZ).replace(tzinfo=None)
    horizon = now + timedelta(days=HIO_HORIZON_DAYS)
    relevant = df[df["_interconnectors"].apply(len) > 0]

    seen = nw.read_state(HIO_STATE_FILE, default={})

    # AEMO's own feed can carry more than one row for the same outage (e.g. a
    # "Planned - SUBMIT" row and a "Planned - RESUBMIT" row both present at once
    # for the identical interconnector/asset/start/finish) - group first and combine
    # every distinct status seen for a key into one sorted string, so the result is
    # deterministic regardless of which row pandas iterates first. Without this, a
    # plain dict overwrite picked whichever duplicate row came last, which could
    # differ between runs and fire a false CHANGED alert on unchanged source data.
    grouped: dict[str, dict] = {}
    for _, row in relevant.iterrows():
        try:
            start_dt = datetime.strptime(row["Start"], "%d-%m-%Y %H:%M")
        except (ValueError, TypeError):
            continue
        if start_dt > horizon:
            continue
        for ic_id in row["_interconnectors"]:
            key = f"{ic_id}|{row.get('Network Asset')}|{row['Start']}|{row.get('Finish')}"
            entry = grouped.setdefault(key, {
                "ic_id": ic_id,
                "asset": row.get("Network Asset"),
                "region": row.get("Region"),
                "nsp": row.get("NSP"),
                "start": row["Start"],
                "finish": row.get("Finish"),
                "statuses": set(),
            })
            entry["statuses"].add(str(row.get("Status") or ""))

    current: dict[str, str] = {}
    alerts = []
    for key, entry in grouped.items():
        status = " / ".join(sorted(entry["statuses"]))
        current[key] = status
        prev_status = seen.get(key)
        label = INTERCONNECTOR_LABELS.get(entry["ic_id"], entry["ic_id"])
        if prev_status is None:
            alerts.append(f"NEW: {label} - {entry['asset']} [{entry['region']}, {entry['nsp']}]: "
                          f"{entry['start']} to {entry['finish']} ({status})")
        elif prev_status != status:
            alerts.append(f"CHANGED: {label} - {entry['asset']}: {entry['start']} to "
                          f"{entry['finish']} - status now {status} (was {prev_status})")

    nw.write_state(HIO_STATE_FILE, current)
    return alerts


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("interconnector", "interconnector-alerts")
    enter_threshold = cfg.get("interconnector_enter_threshold", ENTER_THRESHOLD)
    exit_threshold = cfg.get("interconnector_exit_threshold", EXIT_THRESHOLD)

    try:
        files = nw.get_latest_files(DISPATCHIS_URL, DISPATCHIS_PATTERN, n=LOOKBACK_FILES)
    except Exception as exc:
        print(f"[interconnector_monitor] ERROR listing NEMWEB directory: {exc}")
        sys.exit(1)

    latest_url = files[-1]
    table = nw.download_and_get_table(latest_url, "DISPATCHINTERCONNECTORRES")
    if table.empty:
        print("[interconnector_monitor] Empty DISPATCHINTERCONNECTORRES table.")
        return

    for col in ("MWFLOW", "EXPORTLIMIT", "IMPORTLIMIT"):
        table[col] = pd.to_numeric(table[col], errors="coerce")

    interval_time = parse_settlementdate(table["SETTLEMENTDATE"].iloc[0])

    state = nw.read_state(STATE_FILE, default={})
    flagged = state.get("flagged", {})
    last_processed = state.get("last_settlementdate")
    if last_processed and parse_settlementdate(last_processed) >= interval_time:
        print("[interconnector_monitor] Already processed this interval.")
        return

    alerts = []
    for _, row in table.iterrows():
        ic_id = str(row["INTERCONNECTORID"]).strip()
        util = utilization(row["MWFLOW"], row["EXPORTLIMIT"], row["IMPORTLIMIT"])
        if util is None:
            continue

        was_flagged = flagged.get(ic_id, False)
        direction = "export" if row["MWFLOW"] >= 0 else "import"

        if util >= enter_threshold and not was_flagged:
            label = INTERCONNECTOR_LABELS.get(ic_id, ic_id)
            star = " ★" if ic_id in PRIORITY_INTERCONNECTORS else ""
            alerts.append(
                f"{label}{star}: {row['MWFLOW']:.0f}MW ({direction}), "
                f"{util*100:.0f}% of limit at {interval_time.strftime('%H:%M')} NEM time"
            )
            flagged[ic_id] = True
        elif util < exit_threshold and was_flagged:
            flagged[ic_id] = False  # cleared - can alert again next time it binds

    state["flagged"] = flagged
    state["last_settlementdate"] = interval_time.strftime("%Y/%m/%d %H:%M:%S")
    nw.write_state(STATE_FILE, state)

    hio_alerts = check_high_impact_outages()

    if not alerts and not hio_alerts:
        print("[interconnector_monitor] No interconnector newly at/near its limit, and no new/changed planned outages.")
        return

    sections = []
    if alerts:
        sections.append(f"Interconnector(s) at/near limit ({enter_threshold*100:.0f}%+):\n" + "\n".join(f"  {a}" for a in alerts))
    if hio_alerts:
        sections.append(f"Planned outage(s) affecting a tracked interconnector (next {HIO_HORIZON_DAYS} days):\n"
                         + "\n".join(f"  {a}" for a in hio_alerts))
    message = "\n\n".join(sections)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title=f"Interconnector: {len(alerts)} constraint(s), {len(hio_alerts)} outage change(s)",
        message=message,
        tags=["twisted_rightwards_arrows"],
    )


if __name__ == "__main__":
    main()
