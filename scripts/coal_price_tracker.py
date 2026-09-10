"""
Script: Black-Coal Capacity Withholding vs International Coal Benchmark

Per your "flag every QED macro force" request - the 2022 crisis was gas AND
coal (Ukraine-war shock to both international benchmarks flowing into
domestic offers, then generators withdrawing capacity: "generators withdrew
capacity -> 406 LOR events -> AEMO suspended entire NEM spot market 15-24
June" per the QED dataset's own Q2 2022 row). gas_spread_tracker.py already
covers the gas half; this covers coal.

Domestic side - NOT a simple average bid price. First implementation tried
a capacity-weighted average across all 10 bid price bands and got a
nonsensical $4,206/MWh headline figure. Checked the real distribution
before shipping that: black-coal bidding is bimodal, not a continuous
market price like gas hubs - ~79% of capacity gets bid near/below $0 (a
"must-run" floor bid to guarantee dispatch) and the rest sits in a
defensive high-price tail (bands running up to $15,000-22,000+/MWh,
rarely intended to actually clear). No capacity-weighted average across
all bands, or even just the sub-$300 bands, produces a meaningful single
"price" - the sub-$300 average alone came out at -$542/MWh, which says
nothing useful either. So instead this tracks the real, meaningful
number: what fraction of black-coal capacity sits in that defensive
>$300/MWh tail - i.e. how much capacity is being priced as reserve/
scarcity supply rather than offered to run. That IS the QED-documented
2022 mechanism (capacity withdrawal into extreme bands preceding LOR
events and the market suspension), not a fabricated price figure.

Computed from yesterday's completed Bidmove_Complete file (BID/
BIDDAYOFFER_D for the day's PRICEBAND1-10 per DUID, joined to BID/
BIDPEROFFER_D for each period's BANDAVAIL1-10 MW) - same file/timing
rebid_reconciler.py already uses, only available for a completed trading
day, never "today" (confirmed live via NEMWEB directory listing in an
earlier session). Latest VERSIONNO per DUID (day offer) / per (DUID,
PERIODID) (period offer) is used - a DUID can rebid multiple times in a
day, only the final version is a genuine day-end bid.

International side: Newcastle thermal coal futures (ICE ticker XAL1:COM,
USD/tonne), scraped live from tradingeconomics.com's embedded
TEChartsMeta JSON the same way gas_spread_tracker.py gets JKM - a
CFD-tracked proxy, not an official settlement price, same caveat. Falls
back to config.json's coal_benchmark.international_newcastle_usd_tonne if
the live scrape fails. No unit-matched "spread" against the domestic
figure - one's a % of capacity, the other's a $/tonne fuel commodity
price; reported side by side, not combined.

Both sides get their own running history file and only report trend
deltas once there's a real prior data point to compare against (same
pattern as coal_fleet_trend.py) - no fabricated "vs last year" on day one.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import nemweb_common as nw
import coal_fleet_trend as cft
import gas_spread_tracker as gst

BIDMOVE_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Bidmove_Complete/"
COAL_PAGE_URL = "https://tradingeconomics.com/commodity/coal"

NEM_TZ = timezone(timedelta(hours=10))
HISTORY_FILE = nw.STATE_DIR / "coal_price_history.csv"

# Same boundary the rest of this system already treats as "normal vs extreme" NEM pricing
# (config.json's cap_strike) - capacity bid above this is being offered as defensive/scarcity
# supply, not genuinely intended to run under normal conditions.
WITHHOLDING_PRICE_THRESHOLD = 300.0

# Heuristic trend thresholds, calibrated the same way as the other QED-commentary rules already
# in notification_recap.py (north-south $40 gap, coal-fleet -5% vs month-ago) - NOT a number
# lifted verbatim from QED text (QED gives no explicit "X% withheld" crisis figure to cite
# directly the way gas_spread's $15/GJ genuinely is).
WITHHOLDING_MONTHOVERMONTH_WATCH_PP = 10.0  # percentage-point rise vs a month ago
INTERNATIONAL_MONTHOVERMONTH_WATCH_PCT = 15.0


def read_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    with open(HISTORY_FILE, newline="") as f:
        return list(csv.DictReader(f))


def append_history(row: dict) -> None:
    file_exists = HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "domestic_pct_above_300", "international_usd_tonne"])
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def closest_entry(history: list[dict], target_date: datetime, field: str, tolerance_days: int = 3) -> dict | None:
    best, best_diff = None, None
    for row in history:
        if not row.get(field):
            continue
        row_date = datetime.strptime(row["date"], "%Y-%m-%d")
        diff = abs((row_date - target_date).days)
        if diff <= tolerance_days and (best_diff is None or diff < best_diff):
            best, best_diff = row, diff
    return best


def get_live_newcastle_coal_price() -> float | None:
    """Live Newcastle thermal coal futures (USD/tonne), scraped from tradingeconomics.com's
    embedded TEChartsMeta JSON blob (ticker XAL1:COM, name "Coal") - same scrape mechanism as
    gas_spread_tracker.get_live_jkm_price(), same CFD-proxy caveat."""
    try:
        resp = requests.get(
            COAL_PAGE_URL,
            timeout=nw.CONFIG.get("request_timeout_seconds", 30),
            headers={"User-Agent": gst.BROWSER_USER_AGENT},
        )
        resp.raise_for_status()
        match = re.search(r"TEChartsMeta\s*=\s*(\[.*?\]);", resp.text)
        if not match:
            print("[coal_price_tracker] WARNING: TEChartsMeta not found on coal page - site may have changed.")
            return None
        for entry in json.loads(match.group(1)):
            if entry.get("name") == "Coal" and entry.get("symbol") == "XAL1:COM":
                return float(entry["value"])
        print("[coal_price_tracker] WARNING: 'Coal' (XAL1:COM) entry not found in TEChartsMeta.")
        return None
    except Exception as exc:
        print(f"[coal_price_tracker] WARNING: live coal price fetch failed ({exc}) - falling back to config.json value.")
        return None


def compute_domestic_withholding(yesterday_compact: str) -> dict | None:
    """% of black-coal ENERGY/GEN bid capacity sitting above WITHHOLDING_PRICE_THRESHOLD -
    defensive/scarcity capacity, not capacity genuinely offered to run. Weighted by actual
    band MW across every period in the day (not just DUID count), using each DUID's final
    (latest-VERSIONNO) day offer price bands joined to each period's final band availability.
    Returns None if no Bidmove_Complete file has published yet."""
    files = nw.list_nemweb_files(BIDMOVE_URL, rf"^PUBLIC_BIDMOVE_COMPLETE_{yesterday_compact}_\d+\.zip$")
    if not files:
        return None

    zip_bytes = nw.download_bytes(files[-1])
    tables = nw.parse_mms_zip(zip_bytes)
    day_offer = nw.get_table(tables, "BIDDAYOFFER_D")
    per_offer = nw.get_table(tables, "BIDPEROFFER_D")

    registry = nw.load_registry()
    if registry.fuel_info is None:
        print("[coal_price_tracker] No fuel registry available - cannot filter to black coal.")
        return None
    fcol = cft.fuel_column(registry.fuel_info)
    if not fcol:
        print("[coal_price_tracker] No fuel column found in registry - cannot filter to black coal.")
        return None
    coal_rows = registry.fuel_info[registry.fuel_info[fcol].str.lower() == "black coal"]
    coal_duids = set(coal_rows["DUID"])
    total_capacity_mw = pd.to_numeric(coal_rows["CAPACITY"], errors="coerce").sum() if "CAPACITY" in coal_rows.columns else None

    price_bands = [f"PRICEBAND{i}" for i in range(1, 11)]
    avail_bands = [f"BANDAVAIL{i}" for i in range(1, 11)]

    day_offer = day_offer[
        (day_offer["BIDTYPE"] == "ENERGY") & (day_offer["DIRECTION"] == "GEN") & (day_offer["DUID"].isin(coal_duids))
    ].copy()
    per_offer = per_offer[
        (per_offer["BIDTYPE"] == "ENERGY") & (per_offer["DIRECTION"] == "GEN") & (per_offer["DUID"].isin(coal_duids))
    ].copy()
    if day_offer.empty or per_offer.empty:
        return None

    day_offer["VERSIONNO"] = pd.to_numeric(day_offer["VERSIONNO"], errors="coerce")
    for col in price_bands:
        day_offer[col] = pd.to_numeric(day_offer[col], errors="coerce")
    # Final day offer only - a DUID can rebid multiple times, the highest VERSIONNO is the
    # genuine day-end price (same "final version wins" reasoning as rebid_reconciler.py).
    day_offer = day_offer.sort_values("VERSIONNO").drop_duplicates("DUID", keep="last")
    price_lookup = day_offer.set_index("DUID")[price_bands].to_dict("index")

    per_offer["VERSIONNO"] = pd.to_numeric(per_offer["VERSIONNO"], errors="coerce")
    for col in avail_bands:
        per_offer[col] = pd.to_numeric(per_offer[col], errors="coerce")
    per_offer = per_offer.sort_values("VERSIONNO").drop_duplicates(["DUID", "PERIODID"], keep="last")

    above_mw = 0.0
    below_mw = 0.0
    for _, row in per_offer.iterrows():
        bands = price_lookup.get(row["DUID"])
        if bands is None:
            continue
        for pband, aband in zip(price_bands, avail_bands):
            mw = row[aband]
            price = bands[pband]
            if pd.isna(mw) or pd.isna(price) or mw <= 0:
                continue
            if price > WITHHOLDING_PRICE_THRESHOLD:
                above_mw += mw
            else:
                below_mw += mw

    total_mw = above_mw + below_mw
    if total_mw <= 0:
        return None
    return {
        "pct_above_300": above_mw / total_mw * 100,
        "duid_count": len(price_lookup),
        "total_capacity_mw": total_capacity_mw,
    }


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("gas_spread", "gas-spread-alerts")  # shares the commodity/fuel-cost topic - same audience/purpose
    coal_cfg = cfg.get("coal_benchmark", {})

    yesterday = (datetime.now(NEM_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_compact = yesterday.replace("-", "")

    withholding = compute_domestic_withholding(yesterday_compact)

    intl_usd_tonne = get_live_newcastle_coal_price()
    intl_is_live = intl_usd_tonne is not None
    if intl_usd_tonne is None:
        intl_usd_tonne = coal_cfg.get("international_newcastle_usd_tonne")  # live scrape failed - fall back

    if withholding is None and intl_usd_tonne is None:
        print(f"[coal_price_tracker] No domestic Bidmove_Complete data for {yesterday} and no international "
              f"price available - nothing to report.")
        return

    history = read_history()
    lines = [f"Black-coal capacity withholding vs international benchmark ({yesterday}):"]

    if withholding is not None:
        cap_note = f" of {withholding['total_capacity_mw']:,.0f}MW registered capacity" if withholding["total_capacity_mw"] else ""
        lines.append(f"  Domestic ({withholding['duid_count']} DUID(s){cap_note}): "
                     f"{withholding['pct_above_300']:.1f}% of black-coal capacity bid above "
                     f"${WITHHOLDING_PRICE_THRESHOLD:.0f}/MWh (defensive/scarcity pricing, not offered to run)")
        month_ago = closest_entry(history, datetime.strptime(yesterday, "%Y-%m-%d") - timedelta(days=30), "domestic_pct_above_300")
        if month_ago:
            delta_pp = withholding["pct_above_300"] - float(month_ago["domestic_pct_above_300"])
            lines.append(f"    vs month-ago ({month_ago['date']}): {delta_pp:+.1f} percentage points")
    else:
        lines.append(f"  Domestic: no Bidmove_Complete data available for {yesterday}.")

    if intl_usd_tonne is not None:
        intl_note = "live, tradingeconomics.com" if intl_is_live else "config fallback - live scrape failed"
        lines.append(f"  International (Newcastle thermal coal futures, {intl_note}): ${intl_usd_tonne:,.2f}/tonne")
        month_ago_intl = closest_entry(history, datetime.strptime(yesterday, "%Y-%m-%d") - timedelta(days=30), "international_usd_tonne")
        if month_ago_intl:
            ref_val = float(month_ago_intl["international_usd_tonne"])
            pct = (intl_usd_tonne - ref_val) / ref_val * 100 if ref_val else None
            if pct is not None:
                lines.append(f"    vs month-ago ({month_ago_intl['date']}): {pct:+.1f}%")
    else:
        lines.append("  International: no live scrape and no config.json fallback set "
                      "(coal_benchmark.international_newcastle_usd_tonne).")

    lines.append(f"\n(No combined domestic-vs-international figure - domestic is % of capacity in the "
                 f"defensive bid tail, international is a $/tonne fuel commodity price; not the same "
                 f"thing, unlike gas which has a real $/GJ figure on both sides. QED history: capacity "
                 f"withdrawal into extreme price bands preceded the Q2 2022 LOR events and the first-ever "
                 f"NEM spot market suspension.)")

    append_history({
        "date": yesterday,
        "domestic_pct_above_300": f"{withholding['pct_above_300']:.2f}" if withholding else "",
        "international_usd_tonne": f"{intl_usd_tonne:.2f}" if intl_usd_tonne is not None else "",
    })

    nw.write_state("coal_price_state.json", {
        "checked_at": datetime.now().isoformat(),
        "date": yesterday,
        "domestic_pct_above_300": withholding["pct_above_300"] if withholding else None,
        "international_usd_tonne": intl_usd_tonne,
        "international_is_live": intl_is_live,
    })

    message = "\n".join(lines)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title="Coal: capacity withholding vs international benchmark",
        message=message,
        tags=["coal"],
    )


if __name__ == "__main__":
    main()
