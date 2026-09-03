"""
Rebid Monitor - Stage 1: Real-time MW drop detector

Watches DISPATCH_UNIT_SCADA (5-min updates) for sudden output drops on
thermal/hydro DUIDs and alerts immediately. Logs every triggered DUID to
scada_drops.csv so tomorrow morning's Stage 2 script (rebid_reconciler.py)
can match each drop against its rebid explanation text.

Fuel filter: this is deliberately restricted to {black coal, brown coal,
hydro, gas} - wind and solar output swings constantly with weather and
would otherwise flood this with false positives that have nothing to do
with a rebid or forced outage. That filter needs Scheduled Plant
Information Fuel(-2).csv in registry/ to work; until that file is supplied,
this script falls back to monitoring every DUID and says so loudly in both
the console output and the alert body, since an unfiltered version of this
script is expected to be noisy.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

import nemweb_common as nw

SCADA_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
SCADA_PATTERN = r"^PUBLIC_DISPATCHSCADA_\d{12}_\d{16}\.zip$"

STATE_FILE = "scada_drop_last_processed.json"
DROPS_LOG = nw.STATE_DIR / "scada_drops.csv"

DROP_THRESHOLD_MW = 100
TARGET_FUELS = {"black coal", "brown coal", "hydro", "gas"}


def parse_settlementdate(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


def fuel_column(fuel_info: pd.DataFrame) -> str | None:
    for candidate in ("FUEL", "Fuel", "FUELTYPE", "FUEL_TYPE"):
        if candidate in fuel_info.columns:
            return candidate
    return None


def append_drops_log(rows: list[dict]) -> None:
    file_exists = DROPS_LOG.exists()
    with open(DROPS_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "time", "duid", "region", "station",
                                                "previous_mw", "current_mw", "drop_mw"])
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("rebid", "rebid-alerts")

    try:
        files = nw.get_latest_files(SCADA_URL, SCADA_PATTERN, n=2)
    except Exception as exc:
        print(f"[scada_drop_monitor] ERROR listing NEMWEB directory: {exc}")
        sys.exit(1)

    if len(files) < 2:
        print("[scada_drop_monitor] Not enough snapshots yet to diff.")
        return

    previous_url, latest_url = files[0], files[1]

    last_processed = nw.read_state(STATE_FILE, default={}).get("latest_url")
    if last_processed == latest_url:
        print("[scada_drop_monitor] Already processed this snapshot pair.")
        return

    prev_df = nw.download_and_get_table(previous_url, "DISPATCH_UNIT_SCADA")
    curr_df = nw.download_and_get_table(latest_url, "DISPATCH_UNIT_SCADA")

    prev_df["SCADAVALUE"] = pd.to_numeric(prev_df["SCADAVALUE"], errors="coerce")
    curr_df["SCADAVALUE"] = pd.to_numeric(curr_df["SCADAVALUE"], errors="coerce")

    merged = prev_df[["DUID", "SCADAVALUE"]].merge(
        curr_df[["DUID", "SCADAVALUE", "SETTLEMENTDATE"]],
        on="DUID", how="inner", suffixes=("_prev", "_curr"),
    )
    merged["drop"] = merged["SCADAVALUE_curr"] - merged["SCADAVALUE_prev"]

    registry = nw.load_registry()
    fuel_filter_active = registry.fuel_info is not None
    if fuel_filter_active:
        fcol = fuel_column(registry.fuel_info)
        if fcol:
            target_duids = set(
                registry.fuel_info.loc[
                    registry.fuel_info[fcol].str.lower().isin(TARGET_FUELS), "DUID"
                ]
            )
            merged = merged[merged["DUID"].isin(target_duids)]
        else:
            fuel_filter_active = False
            print("[scada_drop_monitor] WARNING: fuel registry loaded but no FUEL column found - not filtering.")
    else:
        print("[scada_drop_monitor] NOTE: no fuel registry supplied - monitoring ALL DUIDs "
              "(coal/gas/hydro filter disabled, expect wind/solar noise until the fuel CSV is added).")

    drops = merged[merged["drop"] <= -DROP_THRESHOLD_MW].copy()

    nw.write_state(STATE_FILE, {"latest_url": latest_url, "checked_at": datetime.now().isoformat()})

    if drops.empty:
        print(f"[scada_drop_monitor] No drops >= {DROP_THRESHOLD_MW}MW this interval.")
        return

    drops = registry.enrich(drops, duid_col="DUID")
    interval_time = parse_settlementdate(curr_df["SETTLEMENTDATE"].iloc[0])

    # Grouped by owner (same treatment as customer_watcher.py) - all of one company's drops
    # sit together, rather than interleaved with every other company's by raw MW size.
    drops = drops.copy()
    drops["_owner"] = drops["Owner"].fillna("UNKNOWN") if "Owner" in drops.columns else "UNKNOWN"
    owner_order = (
        drops.assign(_absdrop=drops["drop"].abs())
        .groupby("_owner")["_absdrop"].max()
        .sort_values(ascending=False)
        .index
    )

    log_rows = []
    lines = [f"MW drop(s) >= {DROP_THRESHOLD_MW}MW at {interval_time.strftime('%H:%M')} NEM time"
             + ("" if fuel_filter_active else " [UNFILTERED - all fuel types]") + ":"]
    for owner in owner_order:
        lines.append(f"\n{owner}:")
        owner_drops = drops[drops["_owner"] == owner].sort_values("drop")
        for _, row in owner_drops.iterrows():
            station = row.get("STATIONNAME") or row.get("UNIT_NAME") or ""
            region = row.get("REGIONID") or row.get("REGION") or ""
            fuel = row.get("FUEL") or "?"
            label = row["DUID"] + (f" ({station})" if station else "")
            lines.append(f"  {label} [{region}, {fuel}]: {row['SCADAVALUE_prev']:.0f} -> {row['SCADAVALUE_curr']:.0f}MW "
                         f"({row['drop']:.0f}MW)")
            log_rows.append({
                "date": interval_time.strftime("%Y-%m-%d"),
                "time": interval_time.strftime("%H:%M:%S"),
                "duid": row["DUID"],
                "region": region,
                "station": station,
                "previous_mw": row["SCADAVALUE_prev"],
                "current_mw": row["SCADAVALUE_curr"],
                "drop_mw": row["drop"],
            })

    append_drops_log(log_rows)

    message = "\n".join(lines)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title=f"MW drop x{len(drops)}" + ("" if fuel_filter_active else " [unfiltered]"),
        message=message,
        priority="high",
        tags=["chart_with_downwards_trend"],
    )


if __name__ == "__main__":
    main()
