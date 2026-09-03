"""
Script: Customer Generator Watcher

Real-time monitoring of a specific customer/portfolio's own DUIDs, resolved
from config.json's `customer_portfolios` (owner-name substrings matched
against registry/duid_owner_units_capacity.csv) - no separate DUID list
needed since the owner registry already maps every DUID to its legal owner.

Set `"customer_watch_all": true` in config.json to skip the owner filter
entirely and watch every DUID in the registry instead of one company's
portfolio - i.e. every generator in the NEM, all fuel types, at this
script's tighter 20MW-default threshold rather than scada_drop_monitor.py's
100MW/coal-gas-hydro-only one. Worth knowing before turning this on: unlike
scada_drop_monitor.py, this has no fuel filter, so wind/solar farms (which
swing by tens of MW constantly with cloud cover/wind gusts) will fire this
regularly - it will be considerably noisier than the per-portfolio mode.


IMPORTANT SCOPE NOTE - read before relying on this:
The original build doc's spec for this script also wanted "which DUID set
the marginal price each interval" from a "NemPriceSetter XML... every 5
min" feed. That feed does not exist on NEMWEB's real-time tier - verified
live: AEMO's NEMDE solution files (Data_Archive/Wholesale_Electricity/NEMDE/)
publish monthly, roughly two weeks after month-end, in ~5GB zips per month.
There is no free, real-time price-setter identification source. This
script therefore only covers the MW-movement half of the original spec
(reusing the same SCADA-diff pattern as scada_drop_monitor.py, scoped to
the customer's own DUIDs and a tighter default threshold since a customer
portfolio's units are often smaller than the 100MW market-wide threshold).
Price-setter alerting would need either a paid data feed or reverse-
engineering AEMO's live dashboard - not attempted here.
"""

from __future__ import annotations

import sys
from datetime import datetime

import pandas as pd

import nemweb_common as nw

SCADA_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
SCADA_PATTERN = r"^PUBLIC_DISPATCHSCADA_\d{12}_\d{16}\.zip$"

STATE_FILE = "customer_watcher_last_processed.json"


def parse_settlementdate(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


def resolve_customer_duids(owner_capacity: pd.DataFrame, portfolios: list[str]) -> set[str]:
    if owner_capacity is None or not portfolios:
        return set()
    owner_col = next((c for c in owner_capacity.columns if c.lower() == "owner"), None)
    if owner_col is None:
        return set()
    pattern = "|".join(p.lower() for p in portfolios)
    matched = owner_capacity[owner_capacity[owner_col].str.lower().str.contains(pattern, na=False, regex=True)]
    return set(matched["DUID"])


def resolve_all_duids(owner_capacity: pd.DataFrame) -> set[str]:
    if owner_capacity is None or "DUID" not in owner_capacity.columns:
        return set()
    return set(owner_capacity["DUID"].dropna())


def main() -> None:
    cfg = nw.CONFIG
    watch_all = cfg.get("customer_watch_all", False)
    portfolios = cfg.get("customer_portfolios", [])
    threshold = cfg.get("customer_watch_threshold_mw", 20)
    topic = cfg.get("ntfy_topics", {}).get("customer", "customer-alerts")

    registry = nw.load_registry()

    if watch_all:
        customer_duids = resolve_all_duids(registry.owner_capacity)
        label = "ALL DUIDs (customer_watch_all=true)"
    elif portfolios:
        customer_duids = resolve_customer_duids(registry.owner_capacity, portfolios)
        label = f"portfolios {portfolios}"
    else:
        print("[customer_watcher] Neither customer_watch_all nor customer_portfolios is configured - "
              "nothing to watch. Set one in config.json and re-run.")
        return

    if not customer_duids:
        print(f"[customer_watcher] No DUIDs matched ({label}) in duid_owner_units_capacity.csv.")
        return
    print(f"[customer_watcher] Watching {len(customer_duids)} DUID(s) for {label}.")

    try:
        files = nw.get_latest_files(SCADA_URL, SCADA_PATTERN, n=2)
    except Exception as exc:
        print(f"[customer_watcher] ERROR listing NEMWEB directory: {exc}")
        sys.exit(1)

    if len(files) < 2:
        print("[customer_watcher] Not enough snapshots yet to diff.")
        return

    previous_url, latest_url = files[0], files[1]
    last_processed = nw.read_state(STATE_FILE, default={}).get("latest_url")
    if last_processed == latest_url:
        print("[customer_watcher] Already processed this snapshot pair.")
        return

    prev_df = nw.download_and_get_table(previous_url, "DISPATCH_UNIT_SCADA")
    curr_df = nw.download_and_get_table(latest_url, "DISPATCH_UNIT_SCADA")
    prev_df["SCADAVALUE"] = pd.to_numeric(prev_df["SCADAVALUE"], errors="coerce")
    curr_df["SCADAVALUE"] = pd.to_numeric(curr_df["SCADAVALUE"], errors="coerce")

    merged = prev_df[["DUID", "SCADAVALUE"]].merge(
        curr_df[["DUID", "SCADAVALUE", "SETTLEMENTDATE"]],
        on="DUID", how="inner", suffixes=("_prev", "_curr"),
    )
    merged = merged[merged["DUID"].isin(customer_duids)]
    merged["delta"] = merged["SCADAVALUE_curr"] - merged["SCADAVALUE_prev"]
    merged = registry.enrich(merged, duid_col="DUID")

    nw.write_state(STATE_FILE, {"latest_url": latest_url, "checked_at": datetime.now().isoformat()})

    if merged.empty:
        print("[customer_watcher] No customer DUID data this interval.")
        return

    interval_time = parse_settlementdate(curr_df["SETTLEMENTDATE"].iloc[0])
    merged["_fuel"] = merged["FUEL"].fillna("Other")

    # Per-QED-history review (2026-08-27): every single-unit event AEMO's own quarterly
    # reports ever named as price-moving across 35 quarters was a large coal/gas/hydro
    # unit (Torrens Island 120MW up to Bayswater 760MW) - never an individual wind farm,
    # solar farm, or battery, even when much larger. So thermal/hydro get a real per-DUID
    # threshold; wind/solar/battery are summed into one net-movement line each instead,
    # since no single one of those units is individually meaningful the way a coal/gas/
    # hydro unit tripping is.
    INDIVIDUAL_FUELS = {"Black Coal", "Brown Coal", "Gas", "Hydro", "Diesel", "Other"}
    AGGREGATE_FUELS = {"Wind", "Solar", "Battery"}
    AGGREGATE_THRESHOLD_MW = 100

    lines = [f"Customer DUID activity at {interval_time.strftime('%H:%M')} NEM time:"]
    any_content = False

    individual = merged[merged["_fuel"].isin(INDIVIDUAL_FUELS)]
    moves = individual[individual["delta"].abs() >= threshold].copy()
    if not moves.empty:
        any_content = True
        # Grouped by owner (biggest single move within each owner's group determines sort
        # order), not one flat list - so all of e.g. Origin's moves sit together under
        # "Origin Energy", rather than interleaved with every other company's moves.
        moves["_owner"] = moves["Owner"].fillna("UNKNOWN") if "Owner" in moves.columns else "UNKNOWN"
        owner_order = (
            moves.assign(_absdelta=moves["delta"].abs())
            .groupby("_owner")["_absdelta"].max()
            .sort_values(ascending=False)
            .index
        )
        lines.append(f"\nThermal/hydro moves >= {threshold}MW:")
        for owner in owner_order:
            lines.append(f"\n{owner}:")
            owner_moves = moves[moves["_owner"] == owner].sort_values("delta", key=abs, ascending=False)
            for _, row in owner_moves.iterrows():
                station = row.get("STATIONNAME") or row.get("UNIT_NAME") or ""
                region = row.get("REGIONID") or row.get("REGION") or ""
                fuel = row.get("FUEL") or "?"
                sign = "+" if row["delta"] > 0 else ""
                label = row["DUID"] + (f" ({station})" if station else "")
                lines.append(f"  {label} [{region}, {fuel}]: {row['SCADAVALUE_prev']:.0f} -> {row['SCADAVALUE_curr']:.0f}MW "
                             f"({sign}{row['delta']:.0f}MW)")

    aggregate_lines = []
    for fuel in sorted(AGGREGATE_FUELS):
        fuel_rows = merged[merged["_fuel"] == fuel]
        if fuel_rows.empty:
            continue
        net = fuel_rows["delta"].sum()
        if abs(net) >= AGGREGATE_THRESHOLD_MW:
            sign = "+" if net > 0 else ""
            aggregate_lines.append(f"  {fuel}: net {sign}{net:.0f}MW this interval ({len(fuel_rows)} unit(s))")
    if aggregate_lines:
        any_content = True
        lines.append(f"\nWind/solar/battery (aggregate, net >= {AGGREGATE_THRESHOLD_MW}MW):")
        lines.extend(aggregate_lines)

    if not any_content:
        print("[customer_watcher] No customer DUID activity above threshold this interval.")
        return

    message = "\n".join(lines)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title=f"Customer DUID move x{len(moves)}",
        message=message,
        priority="high",
        tags=["bust_in_silhouette"],
    )


if __name__ == "__main__":
    main()
