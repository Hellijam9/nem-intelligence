"""
Market Read - the live cause-and-effect synthesis layer.

Assembles current signals from every other script's state (and a couple of
fresh live pulls) and runs them through causal_rules.py - the QED historical
mechanics encoded as pattern-matching rules - to produce a "this is
happening, here's the pattern it matches, here's the historical precedent"
digest. This is the live equivalent of what the QED report does for
history: map a moved signal back to a plausible cause and what similar
conditions led to in the past.

Not a forecast. It interprets current + recent state against known
patterns; a human still makes the call. Run this every 30-60 minutes,
or on demand.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import nemweb_common as nw
import causal_rules
from cap_dayahead import (
    actual_prices_since_midnight, windowed_prices, parse_price_datetime,
    PREDISPATCH_URL, PREDISPATCH_PATTERN, PREDISPATCH_PERIOD_HOURS,
)

DISPATCHIS_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
DISPATCHIS_PATTERN = r"^PUBLIC_DISPATCHIS_\d{12}_\d{16}\.zip$"

SCADA_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Dispatch_SCADA/"
SCADA_PATTERN = r"^PUBLIC_DISPATCHSCADA_\d{12}_\d{16}\.zip$"

NEM_TZ = timezone(timedelta(hours=10))

# same interconnector-utilization logic as interconnector_monitor.py, kept
# in sync manually since this script reads a fresh pull rather than that
# script's alert state (utilization here reflects right now, not "was it
# ever flagged")
ENTER_THRESHOLD = 0.90


def parse_settlementdate(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


def utilization(flow: float, export_limit: float, import_limit: float) -> float | None:
    limit = export_limit if flow >= 0 else import_limit
    if limit == 0:
        return None
    return abs(flow) / abs(limit)


def closest_history_entry(rows: list[dict], date_col: str, target_date: datetime, tolerance_days: int) -> dict | None:
    best, best_diff = None, None
    for row in rows:
        try:
            row_date = datetime.strptime(row[date_col], "%Y-%m-%d")
        except (KeyError, ValueError):
            continue
        diff = abs((row_date - target_date).days)
        if diff <= tolerance_days and (best_diff is None or diff < best_diff):
            best, best_diff = row, diff
    return best


def read_csv_history(path) -> list[dict]:
    import csv
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def assemble_signals(cfg: dict) -> dict:
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    now = datetime.now(NEM_TZ).replace(tzinfo=None)
    today_str = now.strftime("%Y-%m-%d")

    signals: dict = {"spot_spike_thresholds": cfg.get("spot_spike_thresholds", [300, 1000, 5000, 15000])}

    # --- one fresh DispatchIS pull covers both current prices and interconnector flows ---
    try:
        files = nw.get_latest_files(DISPATCHIS_URL, DISPATCHIS_PATTERN, n=1)
        tables = nw.parse_mms_zip(nw.download_bytes(files[-1]))

        price_df = nw.get_table(tables, "DISPATCHPRICE")
        price_df["RRP"] = pd.to_numeric(price_df["RRP"], errors="coerce")
        signals["current_prices"] = dict(zip(price_df["REGIONID"], price_df["RRP"]))

        ic_df = nw.get_table(tables, "DISPATCHINTERCONNECTORRES")
        for col in ("MWFLOW", "EXPORTLIMIT", "IMPORTLIMIT"):
            ic_df[col] = pd.to_numeric(ic_df[col], errors="coerce")
        flagged = {}
        interconnectors_raw = {}
        for _, row in ic_df.iterrows():
            ic_id = str(row["INTERCONNECTORID"]).strip()
            util = utilization(row["MWFLOW"], row["EXPORTLIMIT"], row["IMPORTLIMIT"])
            flagged[ic_id] = bool(util is not None and util >= ENTER_THRESHOLD)
            interconnectors_raw[ic_id] = {
                "flow": row["MWFLOW"], "export_limit": row["EXPORTLIMIT"],
                "import_limit": row["IMPORTLIMIT"], "utilization": util,
            }
        signals["interconnectors_flagged"] = flagged
        signals["interconnectors_raw"] = interconnectors_raw

        # --- demand, same zip, no extra download - the "heat" half of the classic hot+calm
        # squeeze pattern (SA's Heywood mechanism, and the general extreme-demand driver
        # behind most QED-documented spikes) ---
        demand_df = nw.get_table(tables, "DISPATCHREGIONSUM")
        demand_df["TOTALDEMAND"] = pd.to_numeric(demand_df["TOTALDEMAND"], errors="coerce")
        signals["current_demand"] = dict(zip(demand_df["REGIONID"], demand_df["TOTALDEMAND"]))
    except Exception as exc:
        print(f"[market_read] WARNING: could not fetch live DispatchIS data: {exc}")
        signals["current_prices"] = {}
        signals["interconnectors_flagged"] = {}
        signals["interconnectors_raw"] = {}
        signals["current_demand"] = {}

    # --- quarter-to-date spot average + today/tomorrow expected average - a running average
    # tells you more than one live snapshot does. QTD reuses cap_quarter_to_date.py's own
    # settled price history (no extra fetch). Today/tomorrow reuse cap_dayahead.py's own
    # actual-so-far + Predispatch-remainder blending logic directly (imported, not
    # reimplemented) - just computing a time-weighted average price instead of a cap payout.
    # Time-weighted because actual (5-min) and forecast (30-min) intervals aren't equal length -
    # averaging the raw values equally would under-weight the forecast portion 6x.
    signals["qtd_spot_avg"] = {}
    signals["today_expected_avg"] = {}
    signals["tomorrow_expected_avg"] = {}
    signals["today_cap_payout"] = {}
    signals["tomorrow_cap_payout"] = {}
    strike = cfg.get("cap_strike", 300)
    try:
        quarter_state = nw.read_state("cap_quarter_state.json", default=None)
        if quarter_state:
            for region, prices in quarter_state.get("prices", {}).items():
                if prices:
                    signals["qtd_spot_avg"][region] = sum(prices) / len(prices)

        midnight_tonight = datetime(now.year, now.month, now.day) + timedelta(days=1)
        midnight_day_after = midnight_tonight + timedelta(days=1)
        actual_prices = actual_prices_since_midnight(regions, now)

        pd_files = nw.get_latest_files(PREDISPATCH_URL, PREDISPATCH_PATTERN, n=1)
        pd_df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(pd_files[-1])), "PDREGION")
        pd_df["RRP"] = pd.to_numeric(pd_df["RRP"], errors="coerce")
        pd_df["_period_dt"] = pd_df["PERIODID"].apply(parse_price_datetime)

        for region in regions:
            a_prices = actual_prices.get(region, [])
            region_pd = pd_df[pd_df["REGIONID"] == region]
            remainder = windowed_prices(region_pd, now, midnight_tonight) if not region_pd.empty else pd.Series(dtype=float)
            tomorrow = windowed_prices(region_pd, midnight_tonight, midnight_day_after) if not region_pd.empty else pd.Series(dtype=float)

            weighted_sum = sum(a_prices) * (5 / 60) + remainder.sum() * PREDISPATCH_PERIOD_HOURS
            weighted_hours = len(a_prices) * (5 / 60) + len(remainder) * PREDISPATCH_PERIOD_HOURS
            if weighted_hours > 0:
                signals["today_expected_avg"][region] = weighted_sum / weighted_hours
            if len(tomorrow) > 0:
                signals["tomorrow_expected_avg"][region] = tomorrow.mean()

            # Cap payout today/tomorrow - same actual+forecast price lists, same cap_settlement()
            # formula and /24 divisor cap_dayahead.py uses, so this figure matches that script's
            # own notification exactly (not a separate/different calculation of the same thing).
            _, actual_payout = nw.cap_settlement(a_prices, strike, interval_hours=5 / 60)
            _, remainder_payout = nw.cap_settlement(remainder, strike, interval_hours=PREDISPATCH_PERIOD_HOURS)
            _, tomorrow_payout = nw.cap_settlement(tomorrow, strike, interval_hours=PREDISPATCH_PERIOD_HOURS)
            signals["today_cap_payout"][region] = (actual_payout + remainder_payout) / 24
            signals["tomorrow_cap_payout"][region] = tomorrow_payout / 24
    except Exception as exc:
        print(f"[market_read] WARNING: could not compute qtd/today/tomorrow spot averages: {exc}")

    # --- wind/solar output as % of registered capacity - the "calm" half of the squeeze
    # pattern. Uses the same Dispatch_SCADA feed scada_drop_monitor.py already reads, just
    # keeping wind/solar (which that script deliberately filters OUT) instead of excluding it ---
    signals["wind_solar_output_pct"] = {}
    try:
        scada_files = nw.get_latest_files(SCADA_URL, SCADA_PATTERN, n=1)
        scada_df = nw.download_and_get_table(scada_files[-1], "DISPATCH_UNIT_SCADA")
        scada_df["SCADAVALUE"] = pd.to_numeric(scada_df["SCADAVALUE"], errors="coerce")

        registry = nw.load_registry()
        if registry.fuel_info is not None and registry.owner_capacity is not None:
            fcol = next((c for c in registry.fuel_info.columns if c.lower() == "fuel"), None)
            if fcol:
                vre_duids = set(registry.fuel_info.loc[
                    registry.fuel_info[fcol].str.lower().isin({"wind", "solar"}), "DUID"
                ])
                reg_lookup = registry.merged.set_index("DUID") if registry.merged is not None else None
                vre_scada = scada_df[scada_df["DUID"].isin(vre_duids)].copy()
                if reg_lookup is not None and not vre_scada.empty:
                    vre_scada["region"] = vre_scada["DUID"].map(
                        lambda d: reg_lookup.loc[d].get("REGIONID") if d in reg_lookup.index else None
                    )
                    vre_scada["capacity"] = vre_scada["DUID"].map(
                        lambda d: reg_lookup.loc[d].get("Nameplate Capacity (MW)") if d in reg_lookup.index else None
                    )
                    vre_scada["capacity"] = pd.to_numeric(vre_scada["capacity"], errors="coerce")
                    for region, group in vre_scada.groupby("region"):
                        total_capacity = group["capacity"].sum()
                        total_output = group["SCADAVALUE"].sum()
                        if total_capacity and total_capacity > 0:
                            signals["wind_solar_output_pct"][region] = round(total_output / total_capacity * 100, 1)
    except Exception as exc:
        print(f"[market_read] WARNING: could not compute wind/solar output: {exc}")

    # --- forward weather forecast (next 3 days) per region's demand centre - the genuinely
    # forward-looking half of the picture, since demand/wind above are both "right now" only.
    # BOM's own anonymous FTP/data feed is blocked from this connection (403) - open-meteo.com
    # serves BOM's own ACCESS-G model for Australia free, no key, but that specific endpoint
    # returned nulls for these coordinates (a model coverage/parameter issue), so this uses
    # open-meteo's general forecast endpoint instead - still real forecast data, just not
    # guaranteed to be BOM's own model specifically. One capital city per region as a proxy
    # for that region's demand centre, not a full weather model integration.
    signals["weather_forecast"] = {}
    try:
        region_coords = {
            "NSW1": (-33.87, 151.21), "VIC1": (-37.81, 144.96), "QLD1": (-27.47, 153.03),
            "SA1": (-34.93, 138.60), "TAS1": (-42.88, 147.33),
        }
        lats = ",".join(str(c[0]) for c in region_coords.values())
        lons = ",".join(str(c[1]) for c in region_coords.values())
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lats, "longitude": lons, "daily": "temperature_2m_max,temperature_2m_min,wind_speed_10m_max",
                    "timezone": "Australia/Sydney", "forecast_days": 3},
            timeout=cfg.get("request_timeout_seconds", 30),
        )
        resp.raise_for_status()
        weather_data = resp.json()
        for region, day_data in zip(region_coords, weather_data):
            daily = day_data.get("daily", {})
            signals["weather_forecast"][region] = {
                "dates": daily.get("time", []),
                "max_temp": daily.get("temperature_2m_max", []),
                "min_temp": daily.get("temperature_2m_min", []),
                "max_wind_kmh": daily.get("wind_speed_10m_max", []),
            }
    except Exception as exc:
        print(f"[market_read] WARNING: could not fetch weather forecast: {exc}")

    # --- forward interconnector capacity (STPASA INTERCONNECTORSOLN, ~7 days ahead) - the
    # transmission-side equivalent of pasa_monitor.py's generator-side outage tracking. AEMO's
    # own forward-looking calculated transfer limits per interconnector, compared against
    # today's actual current limit to flag a likely planned de-rating/outage (a big drop, not
    # normal day-to-day loading variation). Nothing tracked this before - PASA/reserve_outlook
    # only cover generator (DUID) availability, not the transmission links themselves.
    signals["interconnector_forecast_reductions"] = []
    try:
        stpasa_files = nw.get_latest_files(
            "https://www.nemweb.com.au/REPORTS/CURRENT/Short_Term_PASA_Reports/",
            r"^PUBLIC_STPASA_\d{12}_\d+\.zip$", n=1,
        )
        ic_soln = nw.get_table(nw.parse_mms_zip(nw.download_bytes(stpasa_files[-1])), "INTERCONNECTORSOLN")
        ic_soln["_interval_dt"] = ic_soln["INTERVAL_DATETIME"].apply(parse_price_datetime)
        ic_soln["_day"] = ic_soln["_interval_dt"].dt.date
        for col in ("CALCULATEDEXPORTLIMIT", "CALCULATEDIMPORTLIMIT"):
            ic_soln[col] = pd.to_numeric(ic_soln[col], errors="coerce")

        today_date = now.date()
        for ic_id, group in ic_soln.groupby("INTERCONNECTORID"):
            current = signals.get("interconnectors_raw", {}).get(ic_id)
            if not current:
                continue
            current_export = abs(current.get("export_limit") or 0)
            current_import = abs(current.get("import_limit") or 0)
            for day, day_df in group.groupby("_day"):
                if day <= today_date:
                    continue
                # Negative CALCULATEDEXPORTLIMIT/IMPORTLIMIT values genuinely occur in this
                # data (confirmed live - not a parsing artifact) - almost certainly meaning
                # that direction is infeasible/reversed under that interval's constraint
                # scenario, not a real capacity magnitude. Excluded rather than abs()'d (which
                # was a real bug here - abs() previously fabricated impossible readings like
                # ">100% reduction" from these). If every interval that day is negative, this
                # day is skipped for that side entirely rather than guessing at what it means.
                export_vals = day_df.loc[day_df["CALCULATEDEXPORTLIMIT"] >= 0, "CALCULATEDEXPORTLIMIT"]
                import_vals = day_df.loc[day_df["CALCULATEDIMPORTLIMIT"] <= 0, "CALCULATEDIMPORTLIMIT"].abs()
                min_export = export_vals.min() if not export_vals.empty else None
                min_import = import_vals.min() if not import_vals.empty else None
                # A likely planned de-rating: that day's tightest calculated limit is well
                # below today's actual limit (30%+ reduction, on a side that's meaningfully
                # sized to begin with). This 30% cutoff is a reasonable first-pass estimate,
                # not validated against a historical baseline of normal day-to-day variation.
                if current_export > 50 and min_export is not None and min_export < current_export * 0.7:
                    signals["interconnector_forecast_reductions"].append({
                        "interconnector": ic_id, "date": str(day), "side": "export",
                        "current_limit": current_export, "forecast_limit": float(min_export),
                    })
                if current_import > 50 and min_import is not None and min_import < current_import * 0.7:
                    signals["interconnector_forecast_reductions"].append({
                        "interconnector": ic_id, "date": str(day), "side": "import",
                        "current_limit": current_import, "forecast_limit": float(min_import),
                    })
    except Exception as exc:
        print(f"[market_read] WARNING: could not fetch interconnector forecast: {exc}")

    # --- High Impact Outages (transmission/interconnector planned outages, weeks to years
    # ahead) - genuinely longer-range than STPASA's ~7-day INTERCONNECTORSOLN above. You asked
    # if there was a longer-term feed for this - there is, AEMO just doesn't publish it in the
    # same MMS/PASA family, so it wasn't found until specifically searched for. ---
    signals["high_impact_outages"] = []
    try:
        hio_df = nw.fetch_high_impact_outages()
        relevant = hio_df[hio_df["_interconnectors"].apply(len) > 0]
        horizon = now + timedelta(days=60)
        for _, row in relevant.iterrows():
            try:
                start_dt = datetime.strptime(row["Start"], "%d-%m-%Y %H:%M")
            except (ValueError, TypeError):
                continue
            if start_dt > horizon:
                continue
            signals["high_impact_outages"].append({
                "interconnectors": row["_interconnectors"],
                "region": row.get("Region"), "nsp": row.get("NSP"),
                "asset": row.get("Network Asset"), "start": row["Start"], "finish": row.get("Finish"),
                "status": row.get("Status"),
            })
    except Exception as exc:
        print(f"[market_read] WARNING: could not fetch High Impact Outages: {exc}")

    # --- active spot-spike tiers (from spot_spike.py's own state, since it already tracks episodes) ---
    spot_state = nw.read_state("spot_spike_state.json", default={})
    signals["active_spot_tiers"] = spot_state.get("active_tier", {r: -1 for r in regions})

    # --- today's SCADA drops ---
    drops = read_csv_history(nw.STATE_DIR / "scada_drops.csv")
    signals["scada_drops_today"] = [
        {**d, "drop_mw": float(d["drop_mw"]), "previous_mw": float(d["previous_mw"]), "current_mw": float(d["current_mw"])}
        for d in drops if d.get("date") == today_str
    ]

    # --- negative pricing trend (weekly history) ---
    neg_history = read_csv_history(nw.STATE_DIR / "negative_pricing_history.csv")
    negative_pricing = {}
    if neg_history:
        latest = neg_history[-1]
        for region in regions:
            key = f"{region}_pct"
            if key not in latest or not latest[key]:
                continue
            pct = float(latest[key])
            month_ref = closest_history_entry(neg_history[:-1], "week_ending", now - timedelta(days=30), 7)
            trend = ""
            if month_ref and month_ref.get(key):
                delta = pct - float(month_ref[key])
                trend = f"vs ~month ago: {float(month_ref[key]):.1f}% ({delta:+.1f}pp)"
            negative_pricing[region] = {"pct": pct, "trend": trend}
    signals["negative_pricing"] = negative_pricing

    # --- coal fleet trend ---
    coal_history = read_csv_history(nw.STATE_DIR / "coal_fleet_history.csv")
    coal_fleet_trend = {}
    if coal_history:
        latest = coal_history[-1]
        current_mw = float(latest["avg_available_mw"])
        # Only compare against a history entry recorded with the SAME fuel_filtered setting -
        # otherwise an old unfiltered all-fleet figure gets compared against today's coal-only
        # one, producing a fake swing (the exact bug already found and fixed inside
        # coal_fleet_trend.py's own closest_entry() - this is a separate implementation here
        # that never got the same fix, found live: an old 44,814MW unfiltered entry got
        # compared against today's 19,075MW coal-only figure, showing a fake "-57.4%").
        same_filter_history = [r for r in coal_history[:-1] if r.get("fuel_filtered") == latest.get("fuel_filtered")]
        month_ref = closest_history_entry(same_filter_history, "date", now - timedelta(days=30), 5)
        month_ago_pct = None
        if month_ref:
            ref_val = float(month_ref["avg_available_mw"])
            if ref_val:
                month_ago_pct = (current_mw - ref_val) / ref_val * 100
        coal_fleet_trend = {
            "current_mw": current_mw,
            "month_ago_pct": month_ago_pct,
            "fuel_filtered": latest.get("fuel_filtered") == "True",
        }
    signals["coal_fleet_trend"] = coal_fleet_trend

    # --- gas spread (persisted by gas_spread_tracker.py) ---
    gas_state = nw.read_state("gas_spread_state.json", default=None)
    if gas_state:
        # jkm_is_live means the JKM figure was scraped fresh this run - genuinely can't be
        # stale. Only fall back to checking benchmark_last_updated's age when jkm_is_live is
        # missing (old state file, pre-automation) or explicitly False (live scrape failed,
        # running on the static config.json fallback value).
        if gas_state.get("jkm_is_live"):
            stale = False
        elif gas_state.get("benchmark_last_updated"):
            age = (datetime.now() - datetime.strptime(gas_state["benchmark_last_updated"], "%Y-%m-%d")).days
            stale = age > 14
        else:
            stale = False
        signals["gas_spread"] = {**gas_state, "stale": stale}
    else:
        signals["gas_spread"] = None

    # --- upcoming PASA outage windows (persisted by pasa_monitor.py) ---
    pasa_cache = nw.read_state("pasa_recent_windows.json", default=[])
    horizon = now + timedelta(days=30)
    signals["pasa_upcoming_windows"] = [
        w for w in pasa_cache
        if datetime.strptime(w["start"], "%Y-%m-%d") <= horizon
        and datetime.strptime(w["end"], "%Y-%m-%d") >= now
    ]

    # --- closure notice matches (persisted by closure_watcher.py) - used by the regional
    # outlook section below to know if NSW's Eraring / VIC's Loy Yang A "coiled spring" calls
    # have actually been resolved by a real announcement, not just left as a static view ---
    signals["closure_matches"] = nw.read_state("closure_watcher_matches.json", default=[])

    return signals


REGIONAL_OUTLOOK_WATCH_PLANTS = {"NSW1": "eraring", "VIC1": "loy yang a"}


def build_regional_outlook(signals: dict) -> str:
    """
    A standing directional view per region, not a live-computed alert - SA/QLD are structural
    theses (repeating SA cap events / QLD's wind-driven cap erosion) with no single trigger
    that cleanly resolves them, so they're shown alongside the real current numbers for you to
    judge yourself, not auto-flipped. NSW/VIC ARE genuinely dynamic though: their whole thesis
    is "cheap now, but an unannounced coal retirement will reprice this the day it's announced"
    - so those two check signals["closure_matches"] (closure_watcher.py's persisted log) for
    an actual mention of Eraring/Loy Yang A and flip from "unpriced risk" to "announced" the
    moment a real notice ever matches.
    """
    matches = signals.get("closure_matches", [])

    def resolved_note(plant_keyword: str) -> str | None:
        for m in matches:
            if plant_keyword in m.get("excerpt", "").lower():
                found_at = m.get("found_at", "?")[:10]
                return f"RESOLVED ({found_at}) - a closure notice mentioning this plant was actually caught. Re-price this view."
        return None

    neg = signals.get("negative_pricing", {}).get("QLD1", {})

    lines = ["\nRegional outlook (standing view, see nem_intelligence_system_remap.md):"]
    lines.append(f"  SA1: BULLISH (caps) - structural Heywood/Murraylink squeeze, repeats most summers/winters. "
                 f"Current negative-pricing: {signals.get('negative_pricing', {}).get('SA1', {}).get('pct', 'n/a')}%.")
    lines.append(f"  QLD1: BEARISH (caps) - wind build eroding cap value. Current negative-pricing: {neg.get('pct', 'n/a')}%.")
    for region, plant in REGIONAL_OUTLOOK_WATCH_PLANTS.items():
        resolved = resolved_note(plant)
        label = "NSW1" if region == "NSW1" else "VIC1"
        plant_name = "Eraring" if region == "NSW1" else "Loy Yang A"
        if resolved:
            lines.append(f"  {label}: {resolved}")
        else:
            lines.append(f"  {label}: COILED SPRING - cheap now, {plant_name}'s closure date still unannounced "
                         f"(checked live against closure_watcher.py's log).")
    return "\n".join(lines)


INTERCONNECTOR_LABELS = {
    "NSW1-QLD1": "QNI", "N-Q-MNSP1": "Terranora", "VIC1-NSW1": "VIC-NSW",
    "V-SA": "Heywood", "V-S-MNSP1": "Murraylink", "T-V-MNSP1": "Basslink",
}

# (region_when_positive, region_when_negative) - i.e. positive MWFLOW = first region -> second
# region named in the interconnector ID, negative = reverse. Verified empirically (not
# assumed from the ID naming alone, since AEMO's own convention isn't guaranteed uniform): on
# 2026-08-24, all 5 interconnectors with nonzero flow showed power moving toward whichever
# region was pricier at that moment, consistently matching this first-named/second-named rule.
INTERCONNECTOR_DIRECTIONS = {
    "NSW1-QLD1": ("NSW1", "QLD1"),
    "N-Q-MNSP1": ("NSW1", "QLD1"),
    "VIC1-NSW1": ("VIC1", "NSW1"),
    "V-SA": ("VIC1", "SA1"),
    "V-S-MNSP1": ("VIC1", "SA1"),
    "T-V-MNSP1": ("TAS1", "VIC1"),
}


def flow_direction_label(ic_id: str, flow: float) -> str:
    pair = INTERCONNECTOR_DIRECTIONS.get(ic_id)
    if pair is None or flow == 0:
        return ""
    frm, to = pair if flow > 0 else (pair[1], pair[0])
    return f"{frm} -> {to}"


def format_raw_data(signals: dict, compact: bool = False) -> str:
    lines = ["=== RAW DATA ==="]

    lines.append("\nSpot prices:")
    all_regions = sorted(set(signals.get("current_prices", {})) | set(signals.get("qtd_spot_avg", {})))
    for region in all_regions:
        qtd = signals.get("qtd_spot_avg", {}).get(region)
        today = signals.get("today_expected_avg", {}).get(region)
        tomorrow = signals.get("tomorrow_expected_avg", {}).get(region)
        parts = []
        if qtd is not None:
            parts.append(f"qtd avg ${qtd:,.2f}")
        if today is not None:
            parts.append(f"today ~${today:,.2f}")
        if tomorrow is not None:
            parts.append(f"tomorrow ~${tomorrow:,.2f}")
        lines.append(f"  {region}: " + (", ".join(parts) if parts else "no data"))

    lines.append("\nCap payout ($300 strike, per 1MW held):")
    for region in all_regions:
        today_payout = signals.get("today_cap_payout", {}).get(region)
        tomorrow_payout = signals.get("tomorrow_cap_payout", {}).get(region)
        parts = []
        if today_payout is not None:
            parts.append(f"today ${today_payout:,.2f}")
        if tomorrow_payout is not None:
            parts.append(f"tomorrow ${tomorrow_payout:,.2f}")
        lines.append(f"  {region}: " + (", ".join(parts) if parts else "no data"))

    # Live interconnector flow/limit display removed from market_read entirely - it duplicates
    # interconnector_monitor.py's own dedicated notification, which already alerts on anything
    # actually at/near its limit. The underlying interconnectors_raw/interconnectors_flagged
    # signals are kept (not removed) since causal_rules.py's SA/Heywood constraint finding
    # still depends on them.

    # These two sections had no cap at all until now - a 7-day window can carry one reduction
    # entry per day per side per interconnector (41 seen live) and a 60-day HIO window easily
    # carries 20+ rows, which is exactly what pushed the whole message over ntfy's ~4KB limit
    # and made it silently turn into an unreadable file attachment instead of a real push.
    # Both already have their own dedicated alert (interconnector_monitor.py fires on anything
    # NEW or CHANGED here) - so in compact mode this is just a one-line pointer, not a repeat
    # of that detail on every single market-read push.
    reductions = signals.get("interconnector_forecast_reductions", [])
    if reductions:
        if compact:
            worst = max(reductions, key=lambda r: (1 - r["forecast_limit"] / r["current_limit"]) if r["current_limit"] else 0)
            worst_pct = (1 - worst["forecast_limit"] / worst["current_limit"]) * 100 if worst["current_limit"] else 0
            worst_label = INTERCONNECTOR_LABELS.get(worst["interconnector"], worst["interconnector"])
            lines.append(f"\nInterconnector capacity reductions ahead (next 7 days): {len(reductions)}, "
                         f"worst {worst_label} {worst_pct:.0f}% down on {worst['date']} (see interconnector alerts for changes)")
        else:
            MAX_RAW_REDUCTIONS = 5
            by_severity = sorted(reductions, key=lambda r: (1 - r["forecast_limit"] / r["current_limit"]) if r["current_limit"] else 0, reverse=True)
            lines.append(f"\nUpcoming interconnector capacity reductions (next 7 days, vs today's actual limit"
                         + (f", {len(reductions)} total, largest {MAX_RAW_REDUCTIONS} shown" if len(reductions) > MAX_RAW_REDUCTIONS else "") + "):")
            for r in by_severity[:MAX_RAW_REDUCTIONS]:
                label = INTERCONNECTOR_LABELS.get(r["interconnector"], r["interconnector"])
                pct = (1 - r["forecast_limit"] / r["current_limit"]) * 100 if r["current_limit"] else 0
                lines.append(f"  {label} ({r['interconnector']}) {r['side']}: {r['date']} - "
                             f"{r['current_limit']:,.0f}MW -> {r['forecast_limit']:,.0f}MW ({pct:.0f}% down)")
            if len(reductions) > MAX_RAW_REDUCTIONS:
                lines.append(f"  ...and {len(reductions) - MAX_RAW_REDUCTIONS} more")

    hio = signals.get("high_impact_outages", [])
    if hio:
        by_start = sorted(hio, key=lambda o: o["start"])
        if compact:
            soonest = by_start[0]
            ic_labels = ", ".join(INTERCONNECTOR_LABELS.get(i, i) for i in soonest["interconnectors"])
            lines.append(f"\nHigh Impact Outages ahead (next 60 days): {len(hio)}, soonest {ic_labels} "
                         f"{soonest['start']} (see interconnector alerts for changes)")
        else:
            MAX_RAW_HIO = 6
            lines.append(f"\nHigh Impact Outages affecting a tracked interconnector (next 60 days"
                         + (f", {len(hio)} total, soonest {MAX_RAW_HIO} shown" if len(hio) > MAX_RAW_HIO else "") + "):")
            for o in by_start[:MAX_RAW_HIO]:
                ic_labels = ", ".join(INTERCONNECTOR_LABELS.get(i, i) for i in o["interconnectors"])
                lines.append(f"  {ic_labels} - {o['asset']} [{o['region']}, {o['nsp']}]: "
                             f"{o['start']} to {o['finish']} ({o['status']})")
            if len(hio) > MAX_RAW_HIO:
                lines.append(f"  ...and {len(hio) - MAX_RAW_HIO} more")

    # SCADA drops display removed - market_read is forward-looking, and scada_drop_monitor.py
    # already has its own dedicated real-time alert for this. Signal kept for causal_rules.py.

    # Negative pricing display removed - backward-looking trailing-week stat, and
    # negative_pricing_tracker.py already has its own dedicated alert for it. Signal kept:
    # feeds causal_rules.py's finding and the regional outlook's SA1/QLD1 blurbs below.

    coal = signals.get("coal_fleet_trend", {})
    if coal:
        label = "Coal-only" if coal.get("fuel_filtered") else "All-fleet (unfiltered)"
        pct = coal.get("month_ago_pct")
        pct_str = f", {pct:+.1f}% vs ~month ago" if pct is not None else ""
        lines.append(f"\n{label} availability: {coal.get('current_mw', 0):,.0f}MW{pct_str}")

    gas = signals.get("gas_spread")
    if gas:
        stale = " [STALE benchmark]" if gas.get("stale") else ""
        lines.append(f"\nGas: domestic avg ${gas.get('domestic_avg', 0):.2f}/GJ, "
                     f"international-equiv ${gas.get('jkm_aud_gj', 0):.2f}/GJ, "
                     f"spread {gas.get('spread_aud_gj', 0):+.2f}/GJ{stale}")

    windows = signals.get("pasa_upcoming_windows", [])
    if windows and not compact:
        # Capped the same way as causal_rules.py's PASA finding - a normal 3-hourly diff is a
        # handful of windows, but a run after a gap (tasks down for days) can otherwise dump
        # dozens/hundreds of lines here. Dropped entirely in compact mode: the commentary
        # section's own PASA finding already covers the same data (grouped by region, with
        # the largest windows), so printing it twice was pure duplication in the push.
        MAX_RAW_WINDOWS = 10
        by_size = sorted(windows, key=lambda w: abs(w["delta"]), reverse=True)
        lines.append(f"\nUpcoming PASA outage windows (next 30 days, {len(windows)} total"
                     + (f", largest {MAX_RAW_WINDOWS} shown" if len(windows) > MAX_RAW_WINDOWS else "") + "):")
        for w in by_size[:MAX_RAW_WINDOWS]:
            lines.append(f"  {w['duid']} ({w.get('station') or '?'}) [{w['region']}]: "
                         f"{w['delta']:+.0f}MW, {w['start']} to {w['end']}")
        if len(windows) > MAX_RAW_WINDOWS:
            lines.append(f"  ...and {len(windows) - MAX_RAW_WINDOWS} more")

    return "\n".join(lines)


def format_commentary(findings: list[causal_rules.Finding], compact: bool = False) -> str:
    if not findings:
        return "No notable patterns matched right now - conditions look unremarkable against the QED history."

    order = {"notable": 0, "watch": 1, "info": 2}
    findings = sorted(findings, key=lambda f: order.get(f.severity, 3))
    absorbed_headlines = set()

    if compact:
        # "info" is this system's own lowest-priority label - dropping it from the actual push
        # is what makes the notification read as "what's important" instead of a full dump of
        # everything that was checked. Full detail (including info-level) is still in the
        # console output when the script is run directly.
        significant = [f for f in findings if f.severity != "info"]
        if not significant:
            return "No notable or watch-level patterns matched right now - only lower-priority info-level signals."
        findings = significant

        # rule_compound_risk() lists its precursor findings by headline inside its own detail
        # text (with a per-region breakdown), so repeating their full explanatory rationale
        # again right below it was pure repetition. But their own detail - e.g. the PASA
        # finding's actual list of which plant, how much MW, what dates - is NOT repeated
        # anywhere else (High Impact Outages only covers transmission/interconnector assets,
        # never generators), so that concrete list still needs to survive. Absorbed findings
        # keep their headline+detail, just skip the redundant Pattern/precedent line.
        compound = next((f for f in findings if f.severity == "notable" and "precursor patterns active" in f.headline), None)
        if compound:
            absorbed_headlines = {
                line.strip().lstrip("-").strip()
                for line in compound.detail.split("\n")
                if line.strip().startswith("-")
            }

    lines = [f"=== MARKET READ ({len(findings)} pattern(s)) ===\n"]
    # Plain text tags, not emoji - "info source" (ℹ️) in particular is a text-style
    # character with a variation selector that a lot of notification-app fonts
    # render as a tofu/question-mark box instead of the intended glyph. Safer to
    # not gamble on emoji rendering at all here.
    icons = {"notable": "[NOTABLE]", "watch": "[WATCH]", "info": "[INFO]"}
    for f in findings:
        lines.append(f"{icons.get(f.severity, '')} {f.headline}")
        # Indent every line of detail, not just the first - some findings (e.g. the grouped
        # PASA outage one) pack multiple lines into a single Finding's detail field.
        detail_lines = f.detail.split("\n")
        if compact and f.severity != "notable":
            # Trim the supporting detail hard for anything below "notable" severity - the
            # compound-risk finding (which carries the per-region weather breakdown you asked
            # to always see) is exempt, since it's the one genuinely important write-up here.
            MAX_COMPACT_DETAIL_LINES = 4
            if len(detail_lines) > MAX_COMPACT_DETAIL_LINES:
                omitted = len(detail_lines) - MAX_COMPACT_DETAIL_LINES
                detail_lines = detail_lines[:MAX_COMPACT_DETAIL_LINES] + [f"...({omitted} more line(s), see console output)"]
        for detail_line in detail_lines:
            lines.append(f"   {detail_line}")
        # In compact mode (the actual ntfy push) only the first sentence of the QED-precedent
        # rationale is kept - these paragraphs were a big contributor to the message blowing
        # well past ntfy's ~4KB limit and silently turning into an unreadable file attachment.
        # The full rationale is still there when you run the script directly. Skipped entirely
        # for a finding absorbed into compound risk - its rationale is compound risk's own
        # rationale, already printed there; only this finding's concrete detail (which plant,
        # how much MW, what dates) is unique and needs to survive.
        if f.headline not in absorbed_headlines:
            precedent = f.precedent
            if compact:
                precedent = precedent.split(". ")[0].rstrip(".") + "."
            lines.append(f"   Pattern: {precedent}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("market_read", "market-read")

    signals = assemble_signals(cfg)
    findings = causal_rules.run_all(signals)

    raw_section = format_raw_data(signals)
    outlook_section = build_regional_outlook(signals)
    full_message = raw_section + "\n" + outlook_section + "\n\n" + format_commentary(findings)
    print(full_message)

    compact_raw_section = format_raw_data(signals, compact=True)
    push_message = compact_raw_section + "\n" + outlook_section + "\n\n" + format_commentary(findings, compact=True)

    notable_count = sum(1 for f in findings if f.severity == "notable")
    nw.push_ntfy(
        topic=topic,
        title=f"Market Read: {len(findings)} pattern(s)" + (f", {notable_count} notable" if notable_count else ""),
        message=push_message,
        priority="high" if notable_count else "default",
        tags=["brain"],
    )


if __name__ == "__main__":
    main()
