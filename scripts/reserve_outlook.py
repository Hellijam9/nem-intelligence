"""
Script: 7-Day Reserve Outlook (new)

There's no public AEMO price forecast beyond Predispatch's ~1.5-2 day
horizon - nobody publishes one. This isn't a price forecast either. It's
AEMO's own official region-level reserve-adequacy computation
(Short_Term_PASA_Reports -> REGIONSOLUTION), which genuinely covers a
~7-day-ahead window and is the exact mechanism behind real AEMO
Lack-of-Reserve (LOR) notices. Tight reserve is the precondition for
extreme prices throughout the QED history, so this fills the 2-7 day gap
between Predispatch (real price data) and PASA (outage declarations) with
a forward risk signal - not a dollar figure.

Source: Short_Term_PASA_Reports -> REGIONSOLUTION table (30-min intervals,
all 5 regions, ~7 days ahead - confirmed live: covers from ~tomorrow
morning out to ~7 days later). Key fields used:
  - SURPLUSRESERVE: MW headroom above the region's reserve requirement,
    per interval - the continuous, leading-indicator figure. Shown here
    as each day's tightest (minimum) point, since that's the moment risk
    is highest.
  - LORCONDITION: AEMO's own official Lack-of-Reserve escalation level
    (0/1/2/3) - the same mechanism behind real LOR market notices. Only
    non-zero when AEMO is genuinely forecasting a reserve shortfall, so
    it's rare - the SURPLUSRESERVE trend is what gives earlier warning
    than this does. When it does fire, the alert spells out what the
    level actually means (LOR_DESCRIPTIONS below) rather than just
    flagging "LOR condition" - confirmed against AEMO's own published
    LOR fact sheet / Lack of Reserve Framework reports:
      LOR1 - reserve below the two largest credible risks combined
      LOR2 - reserve below the single largest credible risk (AEMO can direct/RERT)
      LOR3 - forecast supply at or below demand (possible load shedding)

Not built to any old-script precedent - genuinely new, per your explicit
request for a longer-range price-risk view beyond what predispatch/PASA
already cover.

Second section - DUID-level attribution: REGIONSOLUTION's reserve figures
are computed by AEMO from the same underlying per-DUID availability data,
so any DUID-level change that actually matters for reserve adequacy is
already reflected in the region totals above - this section doesn't add
new *tightness* information. What it adds is knowing *which* generator
moved, the same way pasa_monitor.py's DUID-level detail sits on top of a
plain capacity number. Diffs the two most recent STPASA_DUIDAvailability
snapshots (same >=100MW threshold and grouping style as pasa_monitor.py),
day-level minimum GENERATION_PASA_AVAILABILITY per DUID, restricted to
days present in both snapshots (an outer join would show the newly-visible
7th day at the rolling window's edge as a meaningless "new" delta from
zero - not a real declared change, just the window shifting forward).

Only pushes to ntfy if a day's min surplus reserve or LOR level changed for
any region since the last run, or the DUID-level section found a genuine
change - per your request, no repeat notification on a calm, unchanged day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

import nemweb_common as nw

STPASA_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Short_Term_PASA_Reports/"
STPASA_PATTERN = r"^PUBLIC_STPASA_\d{12}_\d+\.zip$"

STPASA_DUID_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/STPASA_DUIDAvailability/"
STPASA_DUID_PATTERN = r"^PUBLIC_STPASA_DUIDAVAILABILITY_\d{12}_\d+\.zip$"
DUID_THRESHOLD_MW = 100

NEM_TZ = timezone(timedelta(hours=10))


def parse_interval_datetime(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


# AEMO's official Lack of Reserve escalation levels (confirmed against AEMO's own LOR
# fact sheet / Lack of Reserve Framework reports, not guessed):
#   LOR1 - reserve is below what's needed to cover the two largest credible risks in the
#          region at once (e.g. losing the two biggest generators/interconnectors
#          simultaneously). AEMO asks generators/large users to volunteer capacity -
#          no market intervention yet, an early warning.
#   LOR2 - reserve is below what's needed to cover even the single largest credible risk
#          (e.g. losing just the biggest generator or interconnector). AEMO can now direct
#          generators or activate RERT (Reliability & Emergency Reserve Trader) - real
#          intervention, not just a request.
#   LOR3 - forecast available supply is at or below actual operational demand.
#          Involuntary load shedding may be required to keep the system secure.
LOR_DESCRIPTIONS = {
    1: "LOR1 - reserve below what's needed to cover the two largest credible risks at once. AEMO asks for voluntary capacity, no intervention yet.",
    2: "LOR2 - reserve below what's needed to cover even the single largest credible risk. AEMO can direct generators or activate emergency reserves (RERT).",
    3: "LOR3 - forecast supply at or below actual demand. Involuntary load shedding may be required.",
}


def fetch_stpasa_duid_daily_min(url: str) -> pd.DataFrame:
    """DUID x day -> that day's minimum declared PASA availability, from one STPASA_DUIDAvailability snapshot."""
    df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(url)), "DUIDAVAILABILITY")
    df["_interval_dt"] = df["INTERVAL_DATETIME"].apply(parse_interval_datetime)
    df["GENERATION_PASA_AVAILABILITY"] = pd.to_numeric(df["GENERATION_PASA_AVAILABILITY"], errors="coerce")
    df["_day"] = df["_interval_dt"].dt.date
    return df.groupby(["DUID", "_day"], as_index=False)["GENERATION_PASA_AVAILABILITY"].min()


def build_interconnector_reduction_lines(ic_df: pd.DataFrame) -> tuple[bool, list[str]]:
    """
    Forecast interconnector capacity reductions over the outlook window - the transmission-side
    equivalent of the DUID-level section below (generator outages), but for the links
    themselves. You asked whether anything tracked future interconnector outages - nothing did.
    Uses THIS SAME INTERCONNECTORSOLN snapshot's own "today" value as the baseline (not a live
    DispatchIS actual limit) so this script stays self-contained to the one STPASA feed it
    already fetches - comparing AEMO's own forecast for today against its forecast for later
    days, not today's forecast against today's real-time actual (market_read.py's version does
    use the live actual limit, since it already fetches DispatchIS anyway).
    """
    if ic_df.empty:
        return False, ["\n(No INTERCONNECTORSOLN data available this run.)"]

    today_date = ic_df["_day"].min()
    lines = ["\nInterconnector capacity outlook (next ~7 days, vs today's own forecast):"]
    found_any = False
    for ic_id, group in ic_df.groupby("INTERCONNECTORID"):
        today_df = group[group["_day"] == today_date]
        if today_df.empty:
            continue
        # Negative CALCULATEDEXPORTLIMIT/IMPORTLIMIT values genuinely occur in this data
        # (confirmed live - not a parsing artifact) - almost certainly meaning that direction
        # is infeasible/reversed under that interval's constraint scenario, not a real capacity
        # magnitude. Excluded rather than abs()'d (abs() was a real bug in an earlier version
        # of this same logic in market_read.py - it fabricated impossible readings like ">100%
        # reduction" from these). If every interval is negative for a day/side, it's skipped
        # rather than guessing at what it means.
        today_export_vals = today_df.loc[today_df["CALCULATEDEXPORTLIMIT"] >= 0, "CALCULATEDEXPORTLIMIT"]
        today_import_vals = today_df.loc[today_df["CALCULATEDIMPORTLIMIT"] <= 0, "CALCULATEDIMPORTLIMIT"].abs()
        today_export = today_export_vals.min() if not today_export_vals.empty else None
        today_import = today_import_vals.min() if not today_import_vals.empty else None
        for day, day_df in group.groupby("_day"):
            if day <= today_date:
                continue
            export_vals = day_df.loc[day_df["CALCULATEDEXPORTLIMIT"] >= 0, "CALCULATEDEXPORTLIMIT"]
            import_vals = day_df.loc[day_df["CALCULATEDIMPORTLIMIT"] <= 0, "CALCULATEDIMPORTLIMIT"].abs()
            min_export = export_vals.min() if not export_vals.empty else None
            min_import = import_vals.min() if not import_vals.empty else None
            # A likely planned de-rating: 30%+ below today's own forecast, on a side that's
            # meaningfully sized to begin with - a first-pass estimate, not validated against
            # a historical baseline of normal day-to-day variation.
            if today_export is not None and today_export > 50 and min_export is not None and min_export < today_export * 0.7:
                pct = (1 - min_export / today_export) * 100
                lines.append(f"  {ic_id} export: {day} - {today_export:,.0f}MW -> {min_export:,.0f}MW ({pct:.0f}% down)")
                found_any = True
            if today_import is not None and today_import > 50 and min_import is not None and min_import < today_import * 0.7:
                pct = (1 - min_import / today_import) * 100
                lines.append(f"  {ic_id} import: {day} - {today_import:,.0f}MW -> {min_import:,.0f}MW ({pct:.0f}% down)")
                found_any = True
    if not found_any:
        lines.append("  none (no interconnector showing a 30%+ forecast capacity reduction vs today)")
    return found_any, lines


def build_duid_change_lines() -> tuple[bool, list[str]]:
    """Returns (has_genuine_changes, lines) - the bool is used to help decide whether to notify."""
    try:
        files = nw.get_latest_files(STPASA_DUID_URL, STPASA_DUID_PATTERN, n=2)
    except Exception as exc:
        return False, [f"\n[reserve_outlook] WARNING: could not list STPASA_DUIDAvailability: {exc}"]

    if len(files) < 2:
        return False, ["\n(Not enough STPASA_DUIDAvailability snapshots yet to diff DUID-level changes.)"]

    prev_min = fetch_stpasa_duid_daily_min(files[0])
    curr_min = fetch_stpasa_duid_daily_min(files[1])
    # Inner join: only compare days present in both snapshots, so the rolling window's
    # far edge (a day newly visible in curr but absent from prev) doesn't show up as a
    # fake "change" from zero.
    merged = prev_min.merge(curr_min, on=["DUID", "_day"], suffixes=("_prev", "_curr"), how="inner")
    merged["delta"] = merged["GENERATION_PASA_AVAILABILITY_curr"] - merged["GENERATION_PASA_AVAILABILITY_prev"]
    changes = merged[merged["delta"].abs() >= DUID_THRESHOLD_MW].copy()

    if changes.empty:
        return False, [f"\nSTPASA DUID-level changes (>= {DUID_THRESHOLD_MW} MW, next ~7 days): none since last snapshot."]

    registry = nw.load_registry()
    reg_lookup = registry.merged.set_index("DUID") if registry.merged is not None else None

    lines = [f"\nSTPASA DUID-level availability changes since last snapshot (>= {DUID_THRESHOLD_MW} MW, next ~7 days):"]
    for duid, group in changes.groupby("DUID"):
        station = owner = region = ""
        if reg_lookup is not None and duid in reg_lookup.index:
            reg_row = reg_lookup.loc[duid]
            station = reg_row.get("STATIONNAME") or reg_row.get("UNIT_NAME") or ""
            owner = reg_row.get("Owner") or ""
            region = reg_row.get("REGION") or ""
        name = station or duid
        lines.append(f"- {duid} | {name} | {owner or 'UNKNOWN'} | {region or 'UNKNOWN'}")
        for _, row in group.sort_values("_day").iterrows():
            sign = "+" if row["delta"] > 0 else ""
            lines.append(
                f"    {row['_day']}: {sign}{row['delta']:.0f} MW  "
                f"(day's min availability now {row['GENERATION_PASA_AVAILABILITY_curr']:.0f} MW)"
            )
    return True, lines


def main() -> None:
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    topic = cfg.get("ntfy_topics", {}).get("reserve_outlook", "reserve-outlook")

    try:
        files = nw.get_latest_files(STPASA_URL, STPASA_PATTERN, n=1)
    except Exception as exc:
        print(f"[reserve_outlook] ERROR listing NEMWEB directory: {exc}")
        return

    stpasa_tables = nw.parse_mms_zip(nw.download_bytes(files[-1]))

    df = nw.get_table(stpasa_tables, "REGIONSOLUTION")
    df["_interval_dt"] = df["INTERVAL_DATETIME"].apply(parse_interval_datetime)
    df["SURPLUSRESERVE"] = pd.to_numeric(df["SURPLUSRESERVE"], errors="coerce")
    df["LORCONDITION"] = pd.to_numeric(df["LORCONDITION"], errors="coerce").fillna(0)
    df["_day"] = df["_interval_dt"].dt.date

    ic_df = nw.get_table(stpasa_tables, "INTERCONNECTORSOLN")
    ic_df["_interval_dt"] = ic_df["INTERVAL_DATETIME"].apply(parse_interval_datetime)
    ic_df["_day"] = ic_df["_interval_dt"].dt.date
    for col in ("CALCULATEDEXPORTLIMIT", "CALCULATEDIMPORTLIMIT"):
        ic_df[col] = pd.to_numeric(ic_df[col], errors="coerce")

    now = datetime.now(NEM_TZ).replace(tzinfo=None)
    lines = [f"7-DAY RESERVE OUTLOOK (as of {now.strftime('%Y-%m-%d %H:%M')} NEM time)"]

    current_values: dict[str, dict[str, list[float]]] = {}
    for region in regions:
        region_df = df[df["REGIONID"] == region]
        if region_df.empty:
            continue
        lines.append(f"- {region}")
        current_values[region] = {}
        for day, day_df in region_df.sort_values("_interval_dt").groupby("_day", sort=False):
            min_surplus = day_df["SURPLUSRESERVE"].min()
            lor_level = int(day_df["LORCONDITION"].max())
            flag = f"  <-- {LOR_DESCRIPTIONS.get(lor_level, f'LOR condition {lor_level} forecast')}" if lor_level > 0 else ""
            lines.append(f"    {day}: min surplus reserve {min_surplus:,.0f} MW{flag}")
            current_values[region][str(day)] = [round(min_surplus, 0), lor_level]

    ic_changed, ic_lines = build_interconnector_reduction_lines(ic_df)
    lines.extend(ic_lines)
    if ic_changed:
        current_values["_interconnector_reductions"] = "\n".join(ic_lines)

    duid_changed, duid_lines = build_duid_change_lines()
    lines.extend(duid_lines)

    message = "\n".join(lines)
    print(message)

    # Only notify if a day's min surplus reserve or LOR level changed for any region, the
    # DUID-level section found a genuine change, or the interconnector-reduction section did -
    # not on every run regardless of content.
    notify_state = nw.read_state("reserve_outlook_notify_state.json", default={})
    if not duid_changed and not ic_changed and notify_state.get("values") == current_values:
        print("[reserve_outlook] No change since last run - not pushing.")
        return

    nw.write_state("reserve_outlook_notify_state.json", {"values": current_values})
    nw.push_ntfy(
        topic=topic,
        title="7-day reserve outlook",
        message=message,
        tags=["calendar", "warning"],
    )


if __name__ == "__main__":
    main()
