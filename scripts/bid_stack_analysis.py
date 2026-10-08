"""
Bid Stack Analysis - daily, next-morning

What it answers
---------------
  * Who is pricing aggressively (capacity offered at $300+, $1,000+, $5,000+) and
    whether that is unusual for them (vs their own trailing baseline).
  * Covers the whole trading day in time-of-day blocks (morning, daytime, evening peak,
    late evening, overnight - see BLOCKS); evening stays the headline.
  * How much cheap supply is left above demand in each block (supply cushion).
  * The bottom of the stack: how often price went to/below $0, MW offered below $0 and at
    the -$1,000 floor by fuel, coal bidding below $0, and storage bidding to charge
    (LOAD-direction energy bids).
  * Who is pivotal - i.e. the region cannot meet demand without them, so they can
    set price if they choose.
  * A forward read for each upcoming block: predispatch forecast demand set
    against the offer stack each portfolio submitted for the same time yesterday.

Method (sourced, not invented)
------------------------------
  * Data: AEMO next-day public files, same sources UNSW-CEEM's nem-bidding-dashboard
    uses - Bidmove_Complete (BIDDAYOFFER_D price bands + BIDPEROFFER_D band volumes),
    Next_Day_Dispatch (DISPATCH.UNIT_SOLUTION availability / cleared MW),
    Public_Prices (DREGION: RRP, TOTALDEMAND), current DispatchIS files for
    interconnector import/export limits, latest Predispatch for the forward look.
  * Availability adjustment (nem-bidding-dashboard `adjust_bids_for_availability`):
    band volumes are trimmed from the highest band down until the total equals the
    unit's dispatch AVAILABILITY. This matters most for wind/solar, whose bids
    routinely exceed what the weather allows.
  * Offers aggregated into price ranges per interval and averaged, fixed load treated
    as priced below $0 (AER Wholesale Market Performance Report methods).
  * Offer prices are at the connection point; they're referred to the regional
    reference node by dividing by the unit's transmission loss factor (this is why
    some band-10 prices exceed the market price cap in the raw file).
  * Fuel, region, station and loss factor come from AEMO's own unit registration (MMSDM
    archive: GENUNITS.CO2E_ENERGY_SOURCE via DUALLOC, DUDETAILSUMMARY), cached in
    registry/aemo_units.csv. Units AEMO hasn't classified yet show as "Unclassified".
  * Only ENERGY bids in the GEN direction are counted (bidirectional batteries'
    LOAD side and scheduled loads are demand, not supply).
  * Pivotal supplier test: a simplified AER PST - portfolio P is pivotal in region R
    at interval t if  regional available capacity - P's regional available capacity
    + interconnector import capability  <  regional TOTALDEMAND. Unlike the AER
    version it does not net off forced interconnector flows, so treat it as a screen.
  * NOT a price-setter analysis. AEMO's NEMDE price-setter files only appear in the
    archive ~1 month later, and a single-region stack crossing demand ignores
    interconnectors, constraints and FCAS co-optimisation (WattClarity: a single
    marginal unit sets price in only a small minority of intervals). The
    "stack-implied price" series is shown purely as a sanity check on the stack.

Run once daily after Bidmove_Complete publishes (~05:00-05:21 NEM).
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import nemweb_common as nw

BIDMOVE_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Bidmove_Complete/"
NEXTDAY_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Next_Day_Dispatch/"
PRICES_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Public_Prices/"
DISPATCHIS_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
ARCHIVE_DISPATCHIS_URL = "https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"
PREDISPATCH_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Predispatch_Reports/"

REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]
PEAK_START, PEAK_END = "17:00", "20:30"   # interval-ending times, NEM time, inclusive of end
# The whole trading day (04:05 -> 04:00 next day) split into time-of-day blocks, each analysed on its
# own: (key, label, start, end) in minutes after midnight of the trading date, interval-ending,
# start exclusive / end inclusive. "evening" is the original 17:00-20:30 window and stays the
# headline (its history rows predate the other blocks).
BLOCKS = [
    ("morning", "Morning", 240, 540),        # 04:00-09:00  pre-dawn + morning ramp
    ("daytime", "Daytime", 540, 1020),       # 09:00-17:00  solar trough
    ("evening", "Evening peak", 1020, 1230), # 17:00-20:30
    ("late", "Late evening", 1230, 1440),    # 20:30-24:00  batteries running down
    ("overnight", "Overnight", 1440, 1680),  # 00:00-04:00  wind vs coal min-load
]
BLOCK_LABEL = {k: lbl for k, lbl, _, _ in BLOCKS}
TROUGH_BLOCKS = {"daytime", "overnight"}   # focus interval = cheapest, not highest demand
FLOOR_RAW = -999.0             # raw (connection-point) offer price counted as "at the floor"
BUCKET_EDGES = [-np.inf, 0, 50, 150, 300, 1000, 5000, np.inf]
BUCKET_LABELS = ["<$0", "$0-50", "$50-150", "$150-300", "$300-1k", "$1k-5k", "$5k+"]
VRE_FUELS = {"Wind", "Solar"}
MIN_PORTFOLIO_MW = 50          # ignore tiny portfolios in the participant tables
BASELINE_DAYS = 20
MIN_BASELINE_DAYS = 5
HISTORY_KEEP_DAYS = 180

INTERCONNECTORS = {            # positive MWFLOW direction: FROM -> TO
    "NSW1-QLD1": ("NSW1", "QLD1"),
    "N-Q-MNSP1": ("NSW1", "QLD1"),
    "VIC1-NSW1": ("VIC1", "NSW1"),
    "V-SA": ("VIC1", "SA1"),
    "V-S-MNSP1": ("VIC1", "SA1"),
    "T-V-MNSP1": ("TAS1", "VIC1"),
}

DOCS_DIR = nw.PROJECT_ROOT / "docs" / "bidstack"
TEMPLATE = nw.PROJECT_ROOT / "scripts" / "bid_stack_template.html"
HISTORY_FILE = nw.STATE_DIR / "bidstack_history.csv"
REGION_HISTORY_FILE = nw.STATE_DIR / "bidstack_region_history.csv"
STATE_FILE = "bidstack_state.json"
TS_FMT = "%Y/%m/%d %H:%M:%S"


def log(msg: str) -> None:
    print(f"[bid_stack] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Streaming MMS reader - the Bidmove file is ~135MB of CSV with ~650k per-offer
# rows across every bid type; only ENERGY/GEN rows are kept, column-pruned.
# ---------------------------------------------------------------------------

def stream_tables(zip_bytes: bytes, specs: dict) -> dict[str, pd.DataFrame]:
    """specs: {TABLE: (columns_to_keep, {COL: allowed_values_set_or_None})}."""
    out: dict[str, list[list[str]]] = {t: [] for t in specs}
    idx: dict[str, dict[str, int]] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(name) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
            current = None
            for row in reader:
                if not row:
                    continue
                rt = row[0]
                if rt == "I":
                    tname = (row[2] or row[1]).strip().upper()
                    current = tname if tname in specs else None
                    if current:
                        idx[current] = {c.strip(): i for i, c in enumerate(row)}
                    continue
                if rt != "D" or current is None:
                    continue
                tname = (row[2] or row[1]).strip().upper()
                if tname != current:
                    continue
                cols, filters = specs[current]
                ix = idx[current]
                ok = True
                for col, allowed in filters.items():
                    i = ix.get(col)
                    val = row[i].strip() if i is not None and i < len(row) else ""
                    if val not in allowed:
                        ok = False
                        break
                if ok:
                    out[current].append([row[ix[c]] if c in ix and ix[c] < len(row) else "" for c in cols])
    return {t: pd.DataFrame(rows, columns=specs[t][0]) for t, rows in out.items()}


def find_file(directory: str, pattern: str) -> str | None:
    files = nw.list_nemweb_files(directory, pattern)
    return files[-1] if files else None


def num(df: pd.DataFrame, cols) -> pd.DataFrame:
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

BANDS = [f"BANDAVAIL{i}" for i in range(1, 11)]
PRICES = [f"PRICEBAND{i}" for i in range(1, 11)]
ENERGY_ANY = {"BIDTYPE": {"ENERGY"}, "DIRECTION": {"GEN", "", "LOAD"}}


def load_bids(day: str):
    """Energy offers (GEN direction) and energy bids to consume (LOAD direction - battery charging,
    pumps, scheduled loads). Returns (gen_day, gen_per, load_day, load_per)."""
    url = find_file(BIDMOVE_URL, rf"^PUBLIC_BIDMOVE_COMPLETE_{day}_\d+\.zip$")
    if not url:
        raise FileNotFoundError(f"Bidmove_Complete for {day} not published yet")
    log(f"downloading {url.rsplit('/', 1)[-1]}")
    t = stream_tables(nw.download_bytes(url), {
        "BIDDAYOFFER_D": (["DUID", "PARTICIPANTID", "DIRECTION"] + PRICES, ENERGY_ANY),
        "BIDPEROFFER_D": (["DUID", "DIRECTION", "INTERVAL_DATETIME", "MAXAVAIL", "FIXEDLOAD"] + BANDS, ENERGY_ANY),
    })
    dd, pp = t["BIDDAYOFFER_D"], t["BIDPEROFFER_D"]
    is_load_d = dd["DIRECTION"].str.strip() == "LOAD"
    is_load_p = pp["DIRECTION"].str.strip() == "LOAD"
    day_df = num(dd[~is_load_d].drop_duplicates("DUID", keep="last"), PRICES)
    per = num(pp[~is_load_p], ["MAXAVAIL", "FIXEDLOAD"] + BANDS)
    load_day = num(dd[is_load_d].drop_duplicates("DUID", keep="last"), PRICES)
    load_per = num(pp[is_load_p], ["MAXAVAIL", "FIXEDLOAD"] + BANDS)
    log(f"bids: {len(day_df)} DUIDs with energy price bands, {len(per)} per-interval volume rows; "
        f"{len(load_day)} DUIDs bidding to consume")
    return day_df, per, load_day, load_per


def load_dispatch(day: str) -> pd.DataFrame | None:
    url = find_file(NEXTDAY_URL, rf"^PUBLIC_NEXT_DAY_DISPATCH_{day}_\d+\.zip$")
    if not url:
        return None
    t = stream_tables(nw.download_bytes(url), {
        "UNIT_SOLUTION": (["SETTLEMENTDATE", "DUID", "TOTALCLEARED", "AVAILABILITY", "CONNECTIONPOINTID"],
                          {"INTERVENTION": {"0"}}),
    })
    df = num(t["UNIT_SOLUTION"], ["TOTALCLEARED", "AVAILABILITY"])
    log(f"dispatch: {len(df)} unit-interval rows")
    return df.rename(columns={"SETTLEMENTDATE": "INTERVAL_DATETIME"})


def load_regions(day: str) -> pd.DataFrame | None:
    url = find_file(PRICES_URL, rf"^PUBLIC_PRICES_{day}0000_\d+\.zip$")
    if not url:
        return None
    t = stream_tables(nw.download_bytes(url), {
        "DREGION": (["SETTLEMENTDATE", "REGIONID", "RRP", "TOTALDEMAND", "DISPATCHABLEGENERATION",
                     "NETINTERCHANGE"], {"INTERVENTION": {"0"}}),
    })
    df = num(t["DREGION"].drop_duplicates(["SETTLEMENTDATE", "REGIONID"]),
             ["RRP", "TOTALDEMAND", "DISPATCHABLEGENERATION", "NETINTERCHANGE"])
    return df.rename(columns={"SETTLEMENTDATE": "INTERVAL_DATETIME"})


def import_capability(ic: pd.DataFrame, time_col: str) -> pd.DataFrame:
    """Sum, per region and interval, the MW each interconnector could carry INTO it."""
    rows = []
    for _, r in ic.iterrows():
        pair = INTERCONNECTORS.get(r["INTERCONNECTORID"])
        if not pair:
            continue
        frm, to = pair
        rows.append((r[time_col], to, max(abs(r["EXPORTLIMIT"]), 0.0)))   # positive direction flows into TO
        rows.append((r[time_col], frm, max(abs(r["IMPORTLIMIT"]), 0.0)))  # negative direction flows into FROM
    out = pd.DataFrame(rows, columns=["INTERVAL_DATETIME", "REGIONID", "IMPORTCAP"])
    return out.groupby(["INTERVAL_DATETIME", "REGIONID"], as_index=False)["IMPORTCAP"].sum()


def load_ic_limits(peak_times: list[datetime]) -> pd.DataFrame | None:
    """Interconnector limits for the evening-peak intervals, from the current DispatchIS files."""
    try:
        files = nw.list_nemweb_files(DISPATCHIS_URL, r"^PUBLIC_DISPATCHIS_\d{12}_\d{16}\.zip$")
    except Exception as exc:
        log(f"WARNING: DispatchIS listing failed ({exc}) - pivotal/cushion tests skipped")
        return None
    wanted = {t.strftime("%Y%m%d%H%M") for t in peak_times}
    picks = [u for u in files if re.search(r"_(\d{12})_", u.rsplit("/", 1)[-1]).group(1) in wanted]
    if not picks:
        log("WARNING: yesterday's DispatchIS files have rolled off - pivotal/cushion tests skipped")
        return None

    def fetch(u):
        try:
            t = nw.get_table(nw.parse_mms_zip(nw.download_bytes(u)), "DISPATCHINTERCONNECTORRES")
            return t[t["INTERVENTION"].astype(str).str.strip() == "0"]
        except Exception as exc:
            log(f"WARNING: {u.rsplit('/', 1)[-1]}: {exc}")
            return None

    with ThreadPoolExecutor(4) as pool:
        frames = [f for f in pool.map(fetch, picks) if f is not None and len(f)]
    if not frames:
        return None
    ic = num(pd.concat(frames), ["IMPORTLIMIT", "EXPORTLIMIT"])
    log(f"interconnector limits: {len(picks)} peak intervals")
    return import_capability(ic, "SETTLEMENTDATE")


def load_ic_limits_archive(day: str, peak_times: list[datetime]) -> pd.DataFrame | None:
    """Same limits from AEMO's daily DispatchIS archive bundles (a zip of each calendar day's 5-min
    zips). The trading day runs past midnight, so the overnight block needs the next day's bundle
    too. The archive lags ~2 days, so this serves backfills and late re-runs."""
    wanted = {t.strftime("%Y%m%d%H%M") for t in peak_times}
    frames = []
    for d in sorted({w[:8] for w in wanted}):
        url = find_file(ARCHIVE_DISPATCHIS_URL, rf"^PUBLIC_DISPATCHIS_{d}\.zip$")
        if not url:
            continue
        with zipfile.ZipFile(io.BytesIO(nw.download_bytes(url))) as outer:
            for name in outer.namelist():
                m = re.search(r"_(\d{12})_", name)
                if not m or m.group(1) not in wanted:
                    continue
                try:
                    t = nw.get_table(nw.parse_mms_zip(outer.read(name)), "DISPATCHINTERCONNECTORRES")
                    frames.append(t[t["INTERVENTION"].astype(str).str.strip() == "0"])
                except Exception as exc:
                    log(f"WARNING: {name}: {exc}")
    if not frames:
        return None
    ic = num(pd.concat(frames), ["IMPORTLIMIT", "EXPORTLIMIT"])
    return import_capability(ic, "SETTLEMENTDATE")


def load_predispatch() -> tuple[pd.DataFrame, pd.DataFrame] | None:
    try:
        url = nw.get_latest_files(PREDISPATCH_URL, r"^PUBLIC_PREDISPATCH_\d{12}_\d{14}_LEGACY\.zip$")[-1]
        tables = nw.parse_mms_zip(nw.download_bytes(url))
        reg = nw.get_table(tables, "PDREGION")
        ic = nw.get_table(tables, "PDINT")
    except Exception as exc:
        log(f"WARNING: predispatch unavailable ({exc}) - forward look skipped")
        return None
    def pricing_run(df):
        if "INTERVENTION" in df.columns:
            df = df[df["INTERVENTION"].astype(str).str.strip().isin(["", "0"])]
        return df.copy()
    reg, ic = pricing_run(reg), pricing_run(ic)
    reg = num(reg, ["RRP", "TOTALDEMAND", "NETINTERCHANGE"])
    ic = num(ic, ["IMPORTLIMIT", "EXPORTLIMIT"])
    reg["PERIOD"] = pd.to_datetime(reg["PERIODID"], format=TS_FMT)
    ic["PERIOD"] = pd.to_datetime(ic["PERIODID"], format=TS_FMT)
    return reg, import_capability(ic, "PERIOD").rename(columns={"INTERVAL_DATETIME": "PERIOD"})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# AEMO's own unit registration, from the monthly MMSDM archive on NEMWeb (the same source
# UNSW-CEEM's tools fall back to - AEMO's registration spreadsheet is Cloudflare-blocked for
# CI runners). Fuel comes from GENUNITS.CO2E_ENERGY_SOURCE (a closed AEMO vocabulary, mapped
# explicitly below), joined DUID -> GENSETID via DUALLOC; region, participant, station and the
# current transmission loss factor come from DUDETAILSUMMARY. Nothing is inferred from names:
# a unit AEMO hasn't classified yet (newer than the latest monthly archive) is "Unclassified".
MMSDM_BASE = "https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/"
AEMO_UNITS_CACHE = nw.REGISTRY_DIR / "aemo_units.csv"
AEMO_UNITS_META = "aemo_units_meta.json"
AEMO_UNITS_VERSION = 2   # bump when the mapping below changes, to force a rebuild of the cache
ENERGY_SOURCE_FUEL = {
    "black coal": "Black Coal",
    "brown coal": "Brown Coal",
    "natural gas (pipeline)": "Gas",
    "coal seam methane": "Gas",
    "coal mine waste gas": "Gas",
    "ethane": "Gas",
    "hydro": "Hydro",
    "battery storage": "Battery",
    "wind": "Wind",
    "solar": "Solar",
    "diesel oil": "Liquid fuel",
    "kerosene - non aviation": "Liquid fuel",
    "landfill biogas methane": "Bioenergy",
    "biomass and industrial materials": "Bioenergy",
    "bagasse": "Bioenergy",
    "other biofuels": "Bioenergy",
    "primary solid biomass fuels": "Bioenergy",
    "other solid fossil fuels": "Other",
}
CP_REGION = {"N": "NSW1", "Q": "QLD1", "V": "VIC1", "S": "SA1", "T": "TAS1"}  # AEMO TNI code convention


def _links(url: str) -> list[str]:
    import requests
    r = requests.get(url, headers=nw.HTTP_HEADERS, timeout=nw.CONFIG["request_timeout_seconds"])
    r.raise_for_status()
    return re.findall(r'href="([^"]+)"', r.text, re.I)


def _latest_mmsdm_data_dir() -> tuple[str, str]:
    from urllib.parse import urljoin
    years = sorted(h for h in _links(MMSDM_BASE) if re.search(r"/\d{4}/$", h))
    for y in reversed(years):
        yurl = urljoin(MMSDM_BASE, y)
        months = sorted(h for h in _links(yurl) if re.search(r"MMSDM_\d{4}_\d{2}/$", h))
        for m in reversed(months):
            data = urljoin(yurl, m) + "MMSDM_Historical_Data_SQLLoader/DATA/"
            try:
                files = _links(data)
            except Exception:
                continue
            if any("DUDETAILSUMMARY" in f for f in files):
                return data, re.search(r"MMSDM_(\d{4}_\d{2})", m).group(1)
    raise RuntimeError("no MMSDM DATA directory found")


def _mmsdm_table(data_url: str, table: str) -> pd.DataFrame:
    from urllib.parse import urljoin
    files = _links(data_url)
    pat = re.compile(rf"(%23|#){table}(%23|#)FILE\d+", re.I)
    picks = [f for f in files if pat.search(f)]
    if not picks:
        raise RuntimeError(f"{table} not in {data_url}")
    frames = []
    for f in picks:
        tables = nw.parse_mms_zip(nw.download_bytes(urljoin(data_url, f)))
        frames.append(nw.get_table(tables, table))
    return pd.concat(frames, ignore_index=True)


def build_aemo_units() -> pd.DataFrame:
    data_url, month = _latest_mmsdm_data_dir()
    log(f"refreshing AEMO unit registration from MMSDM {month}")
    today = datetime.now(nw.NEM_TZ).strftime("%Y/%m/%d %H:%M:%S")

    dus = _mmsdm_table(data_url, "DUDETAILSUMMARY")
    cur = dus[(dus["START_DATE"] <= today) & (dus["END_DATE"] > today)]
    dus = pd.concat([cur, dus[~dus["DUID"].isin(cur["DUID"])]]).sort_values("START_DATE").drop_duplicates("DUID", keep="last")
    dus = dus[["DUID", "REGIONID", "PARTICIPANTID", "STATIONID", "DISPATCHTYPE", "SCHEDULE_TYPE",
               "TRANSMISSIONLOSSFACTOR", "END_DATE"]]

    dual = _mmsdm_table(data_url, "DUALLOC")
    dual["VERSIONNO"] = pd.to_numeric(dual["VERSIONNO"], errors="coerce")
    latest = dual.groupby("DUID")["EFFECTIVEDATE"].transform("max")
    dual = dual[dual["EFFECTIVEDATE"] == latest]
    dual = dual[dual["VERSIONNO"] == dual.groupby("DUID")["VERSIONNO"].transform("max")][["DUID", "GENSETID"]]

    gen = _mmsdm_table(data_url, "GENUNITS")[["GENSETID", "CO2E_ENERGY_SOURCE", "REGISTEREDCAPACITY", "GENSETTYPE"]]
    gen["REGISTEREDCAPACITY"] = pd.to_numeric(gen["REGISTEREDCAPACITY"], errors="coerce").fillna(0)
    g = dual.merge(gen, on="GENSETID", how="left")
    g = g[g["CO2E_ENERGY_SOURCE"].fillna("").str.strip() != ""]
    # A DUID can span several gensets; take the energy source carrying the most registered MW.
    g = (g.groupby(["DUID", "CO2E_ENERGY_SOURCE"], as_index=False)["REGISTEREDCAPACITY"].sum()
         .sort_values("REGISTEREDCAPACITY").drop_duplicates("DUID", keep="last"))

    st = _mmsdm_table(data_url, "STATION")[["STATIONID", "STATIONNAME"]].drop_duplicates("STATIONID", keep="last")
    pa = _mmsdm_table(data_url, "PARTICIPANT")
    pa = pa[["PARTICIPANTID", "NAME"]].drop_duplicates("PARTICIPANTID", keep="last").rename(columns={"NAME": "PARTICIPANTNAME"})

    out = (dus.merge(g[["DUID", "CO2E_ENERGY_SOURCE"]], on="DUID", how="left")
              .merge(st, on="STATIONID", how="left").merge(pa, on="PARTICIPANTID", how="left"))
    out["AEMO_FUEL"] = out["CO2E_ENERGY_SOURCE"].str.strip().str.lower().map(ENERGY_SOURCE_FUEL)
    # Wholesale demand response: AEMO registers these as scheduled LOAD units with no generating
    # set, yet they offer energy in the generating direction (station names carry "WDR").
    wdr = out["AEMO_FUEL"].isna() & out["CO2E_ENERGY_SOURCE"].isna() & (out["DISPATCHTYPE"] == "LOAD")
    out.loc[wdr, "AEMO_FUEL"] = "Demand response"
    unmapped = sorted(set(out.loc[out["CO2E_ENERGY_SOURCE"].notna() & out["AEMO_FUEL"].isna(), "CO2E_ENERGY_SOURCE"]))
    if unmapped:
        log(f"WARNING: new AEMO energy-source values not mapped yet (shown as Unclassified): {unmapped}")
    out["MMSDM_MONTH"] = month
    return out


def load_aemo_units() -> pd.DataFrame | None:
    """Cached in registry/aemo_units.csv; refreshed when AEMO publishes a newer monthly archive."""
    meta = nw.read_state(AEMO_UNITS_META, default={}) or {}
    cached = None
    if AEMO_UNITS_CACHE.exists():
        try:
            cached = pd.read_csv(AEMO_UNITS_CACHE, dtype=str)
        except Exception:
            cached = None
    stale = (cached is None or meta.get("version") != AEMO_UNITS_VERSION
             or meta.get("checked") != datetime.now(nw.NEM_TZ).strftime("%Y-%m-%d"))
    if stale:
        try:
            _, month = _latest_mmsdm_data_dir()
            if cached is None or month != meta.get("month") or meta.get("version") != AEMO_UNITS_VERSION:
                fresh = build_aemo_units()
                fresh.to_csv(AEMO_UNITS_CACHE, index=False)
                cached = fresh.astype(str).replace({"nan": np.nan, "None": np.nan})
            nw.write_state(AEMO_UNITS_META, {"month": month, "version": AEMO_UNITS_VERSION,
                                             "checked": datetime.now(nw.NEM_TZ).strftime("%Y-%m-%d")})
        except Exception as exc:
            log(f"WARNING: AEMO unit registration refresh failed ({exc}) - using cached copy" if cached is not None
                else f"WARNING: AEMO unit registration unavailable ({exc})")
    return cached


_LOGGED: set = set()


def registry_table(day_df: pd.DataFrame, disp: pd.DataFrame | None) -> pd.DataFrame:
    reg = nw.load_registry()
    aemo = load_aemo_units()
    base = pd.DataFrame({"DUID": day_df["DUID"], "PARTICIPANTID": day_df["PARTICIPANTID"]})
    base = base[~base["DUID"].str.startswith("DG_")]   # AEMO dummy generators, not real supply

    if aemo is not None:
        a = aemo[["DUID", "REGIONID", "STATIONNAME", "AEMO_FUEL", "TRANSMISSIONLOSSFACTOR", "PARTICIPANTNAME"]]
        base = base.merge(a.rename(columns={"REGIONID": "A_REGION", "STATIONNAME": "A_STATION",
                                            "TRANSMISSIONLOSSFACTOR": "A_TLF"}), on="DUID", how="left")
    else:
        for c in ["A_REGION", "A_STATION", "AEMO_FUEL", "A_TLF", "PARTICIPANTNAME"]:
            base[c] = np.nan

    if reg.fuel_info is not None:
        fi = reg.fuel_info.drop_duplicates("DUID", keep="last")[
            ["DUID", "REGIONID", "PORTFOLIO", "STATIONNAME", "FUEL", "TransmissionLossFactor"]]
        base = base.merge(fi, on="DUID", how="left")
    else:
        for c in ["REGIONID", "PORTFOLIO", "STATIONNAME", "FUEL", "TransmissionLossFactor"]:
            base[c] = np.nan
    if reg.duid_info is not None:
        di = reg.duid_info.drop_duplicates("DUID", keep="last")
        base = base.merge(di[["DUID", "REGION", "UNIT_NAME"]], on="DUID", how="left")
        base["REGIONID"] = base["REGIONID"].fillna(base["REGION"].astype(str).str.upper().str.rstrip("1") + "1")
        base["STATIONNAME"] = base["STATIONNAME"].fillna(base["UNIT_NAME"])

    # AEMO's registration is the authority; the hand-kept registry/ files fill gaps only.
    base["REGIONID"] = base["A_REGION"].where(base["A_REGION"].isin(REGIONS), base["REGIONID"])
    base["FUEL"] = base["AEMO_FUEL"].fillna(base["FUEL"].replace({"Diesel": "Liquid fuel"}))
    base["STATIONNAME"] = base["A_STATION"].fillna(base["STATIONNAME"])
    base["TLF"] = pd.to_numeric(base["A_TLF"], errors="coerce").fillna(
        pd.to_numeric(base["TransmissionLossFactor"], errors="coerce"))

    # Region only (never fuel) for units newer than both sources: AEMO's transmission node codes
    # begin with the region letter.
    base["INFERRED"] = ~base["REGIONID"].isin(REGIONS)
    if disp is not None and "CONNECTIONPOINTID" in disp.columns:
        cp = disp.drop_duplicates("DUID").set_index("DUID")["CONNECTIONPOINTID"].astype(str).str[:1].map(CP_REGION)
        base.loc[base["INFERRED"], "REGIONID"] = base.loc[base["INFERRED"], "DUID"].map(cp)

    if reg.owner_capacity is not None and "Owner" in reg.owner_capacity.columns:
        base = base.merge(reg.owner_capacity[["DUID", "Owner"]], on="DUID", how="left")
    else:
        base["Owner"] = np.nan
    # Portfolio = trading brand (AGL, Origin Energy, CS Energy...), the level at which bidding
    # strategy is set. A unit traded under a participant ID that already trades branded units
    # inherits that brand; otherwise AEMO's registered participant name, then the legal owner.
    brand = (base.dropna(subset=["PORTFOLIO"]).groupby("PARTICIPANTID")["PORTFOLIO"]
             .agg(lambda x: x.value_counts().index[0]))
    tidy = lambda s: (s.astype("string")
                      .str.replace(r"\s+as (the )?trustee.*$", "", regex=True, case=False)
                      .str.replace(r"\s+(Pty\.? ?Ltd\.?|Pty Limited|Limited|Ltd)\s*$", "", regex=True, case=False)
                      .str.strip())
    base["PORTFOLIO"] = (base["PORTFOLIO"].fillna(base["PARTICIPANTID"].map(brand))
                         .fillna(tidy(base["PARTICIPANTNAME"])).fillna(tidy(base["Owner"]))
                         .fillna(base["PARTICIPANTID"]))
    base["FUEL"] = base["FUEL"].fillna("Unclassified")
    base["STATIONNAME"] = base["STATIONNAME"].fillna(base["DUID"])
    base["TLF"] = base["TLF"].fillna(1.0)
    base.loc[(base["TLF"] < 0.5) | (base["TLF"] > 1.5), "TLF"] = 1.0

    uncl = base[base["FUEL"] == "Unclassified"]
    msg_key = ",".join(sorted(uncl["DUID"]))
    if len(uncl) and msg_key not in _LOGGED:
        _LOGGED.add(msg_key)
        log(f"NOTE: {len(uncl)} DUIDs not yet fuel-classified by AEMO (shown as Unclassified): "
            + ", ".join(sorted(uncl["DUID"])))
    return base[["DUID", "REGIONID", "PORTFOLIO", "STATIONNAME", "FUEL", "TLF", "INFERRED"]]


# ---------------------------------------------------------------------------
# Build the availability-adjusted offer stack (long form: one row per unit-interval-band)
# ---------------------------------------------------------------------------

def build_stack(day_df, per, disp, reg) -> pd.DataFrame:
    df = per.merge(day_df[["DUID"] + PRICES], on="DUID", how="inner")
    if disp is not None:
        df = df.merge(disp[["DUID", "INTERVAL_DATETIME", "AVAILABILITY"]], on=["DUID", "INTERVAL_DATETIME"], how="left")
        cap = np.fmin(df["MAXAVAIL"].to_numpy(), df["AVAILABILITY"].to_numpy())
    else:
        cap = df["MAXAVAIL"].to_numpy()
    cap = np.nan_to_num(np.clip(cap, 0, None))

    vols = np.nan_to_num(df[BANDS].to_numpy(dtype=float)).clip(min=0)
    # Trim from the top band down so the total never exceeds availability.
    below = np.cumsum(vols, axis=1) - vols
    adj = np.clip(np.minimum(vols, cap[:, None] - below), 0, None)

    # Fixed load: unit is dispatched to that MW regardless of price -> priced below $0.
    fixed = np.nan_to_num(df["FIXEDLOAD"].to_numpy(dtype=float))
    is_fixed = fixed > 0
    adj[is_fixed] = 0.0

    df = df.merge(reg, on="DUID", how="left")
    tlf = df["TLF"].fillna(1.0).to_numpy()
    raw = df[PRICES].to_numpy(dtype=float)
    prices = raw / tlf[:, None]   # refer to regional reference node

    n = len(df)
    parts = []
    meta = df[["DUID", "INTERVAL_DATETIME", "REGIONID", "PORTFOLIO", "FUEL"]]
    for k in range(10):
        m = adj[:, k] > 0
        p = meta[m].copy()
        p["PRICE"] = prices[m, k]
        p["RAW"] = raw[m, k]
        p["MW"] = adj[m, k]
        parts.append(p)
    if is_fixed.any():
        p = meta[is_fixed].copy()
        p["PRICE"] = -1000.0
        p["RAW"] = -1000.0
        p["MW"] = np.minimum(fixed[is_fixed], cap[is_fixed])
        parts.append(p)
    stack = pd.concat(parts, ignore_index=True)
    stack = stack[stack["REGIONID"].isin(REGIONS)]
    stack["T"] = pd.to_datetime(stack["INTERVAL_DATETIME"], format=TS_FMT)
    stack["BUCKET"] = pd.cut(stack["PRICE"], BUCKET_EDGES, labels=BUCKET_LABELS, right=False)
    unknown = {d for d in df.loc[~df["REGIONID"].isin(REGIONS), "DUID"] if not d.startswith("DG_")}
    if unknown:
        log(f"NOTE: {len(unknown)} DUIDs have no region in registry/ and were excluded: {sorted(unknown)[:12]}")
    log(f"stack: {len(stack)} unit-interval-band rows from {n} unit-intervals")
    return stack


def build_load_stack(load_day, load_per, reg) -> pd.DataFrame:
    """Bids to CONSUME (battery charging, pumps, scheduled loads), long form like build_stack.
    A load bid at price P means 'consume this MW if the regional price is at or below P'.
    Volumes are trimmed to MAXAVAIL only (dispatch AVAILABILITY is the generating side)."""
    cols = ["DUID", "INTERVAL_DATETIME", "REGIONID", "PORTFOLIO", "FUEL", "PRICE", "MW", "T"]
    if load_per is None or load_per.empty or load_day is None or load_day.empty:
        return pd.DataFrame(columns=cols)
    df = load_per.merge(load_day[["DUID"] + PRICES], on="DUID", how="inner")
    cap = np.nan_to_num(np.clip(df["MAXAVAIL"].to_numpy(dtype=float), 0, None))
    vols = np.nan_to_num(df[BANDS].to_numpy(dtype=float)).clip(min=0)
    below = np.cumsum(vols, axis=1) - vols
    adj = np.clip(np.minimum(vols, cap[:, None] - below), 0, None)
    df = df.merge(reg, on="DUID", how="left")
    tlf = df["TLF"].fillna(1.0).to_numpy()
    prices = df[PRICES].to_numpy(dtype=float) / tlf[:, None]
    meta = df[["DUID", "INTERVAL_DATETIME", "REGIONID", "PORTFOLIO", "FUEL"]]
    parts = []
    for k in range(10):
        m = adj[:, k] > 0
        p = meta[m].copy()
        p["PRICE"] = prices[m, k]
        p["MW"] = adj[m, k]
        parts.append(p)
    out = pd.concat(parts, ignore_index=True)
    out = out[out["REGIONID"].isin(REGIONS)]
    out["T"] = pd.to_datetime(out["INTERVAL_DATETIME"], format=TS_FMT)
    log(f"load bids: {len(out)} unit-interval-band rows")
    return out


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def hm(minutes: int) -> str:
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def block_mask(times: pd.Series, day: datetime, a: int, b: int) -> pd.Series:
    return (times > day + timedelta(minutes=a)) & (times <= day + timedelta(minutes=b))


def peak_mask(times: pd.Series, day: datetime) -> pd.Series:
    return block_mask(times, day, 1020, 1230)


def tod_block(ts) -> str | None:
    """Which block an interval-ending timestamp falls in (by time of day)."""
    m = ts.hour * 60 + ts.minute
    if m <= 240:
        m += 1440
    for k, _, a, b in BLOCKS:
        if a < m <= b:
            return k
    return None


def pivotal_and_cushion(stack_peak, regions_peak, impcap):
    """Per region-interval supply cushion, and per portfolio pivotal intervals (AER PST, simplified)."""
    if impcap is None or regions_peak is None:
        return None, None
    tot = stack_peak.groupby(["REGIONID", "T"])["MW"].sum().rename("AVAIL")
    cheap = stack_peak[stack_peak["PRICE"] < 300].groupby(["REGIONID", "T"])["MW"].sum().rename("AVAIL300")
    r = regions_peak.set_index(["REGIONID", "T"])[["TOTALDEMAND", "RRP"]]
    ic = impcap.assign(T=pd.to_datetime(impcap["INTERVAL_DATETIME"], format=TS_FMT)).set_index(["REGIONID", "T"])["IMPORTCAP"]
    c = pd.concat([tot, cheap, r, ic], axis=1).dropna(subset=["TOTALDEMAND", "IMPORTCAP"]).fillna({"AVAIL300": 0})
    c["CUSHION_PCT"] = (c["AVAIL"] + c["IMPORTCAP"] - c["TOTALDEMAND"]) / c["TOTALDEMAND"] * 100
    c["CHEAP_HEADROOM"] = c["AVAIL300"] + c["IMPORTCAP"] - c["TOTALDEMAND"]

    own = stack_peak.groupby(["REGIONID", "T", "PORTFOLIO"])["MW"].sum().rename("OWN").reset_index()
    own = own.merge(c.reset_index()[["REGIONID", "T", "AVAIL", "IMPORTCAP", "TOTALDEMAND"]], on=["REGIONID", "T"])
    own["RESIDUAL"] = own["AVAIL"] - own["OWN"] + own["IMPORTCAP"]
    own["PIVOTAL"] = own["RESIDUAL"] < own["TOTALDEMAND"]
    own["SHORTFALL"] = (own["TOTALDEMAND"] - own["RESIDUAL"]).clip(lower=0)
    piv = own.groupby(["REGIONID", "PORTFOLIO"]).agg(
        PIVOTAL_N=("PIVOTAL", "sum"), MAX_SHORTFALL=("SHORTFALL", "max")).reset_index()
    return c.reset_index(), piv


def implied_price(stack_region: pd.DataFrame, regions: pd.DataFrame, region: str):
    """Local-stack price where cumulative offers reach the region's dispatched generation. Sanity check only."""
    rr = regions[regions["REGIONID"] == region].set_index("T")
    out = []
    for t, g in stack_region.groupby("T"):
        if t not in rr.index:
            continue
        q = rr.at[t, "DISPATCHABLEGENERATION"]
        g = g.sort_values("PRICE")
        cum = g["MW"].cumsum().to_numpy()
        i = int(np.searchsorted(cum, q))
        p = float(g["PRICE"].iloc[min(i, len(g) - 1)]) if len(g) else math.nan
        out.append((t, p, float(rr.at[t, "RRP"])))
    return out


def curve_points(g: pd.DataFrame, max_points: int = 400):
    g = g.sort_values("PRICE")
    cum = g["MW"].cumsum()
    pts = [{"mw": round(float(c), 1), "p": round(float(p), 2), "o": o}
           for c, p, o in zip(cum, g["PRICE"], g["PORTFOLIO"])]
    if len(pts) > max_points:  # keep every price step, thin within flat runs
        step = len(pts) // max_points + 1
        pts = [pt for i, pt in enumerate(pts) if i % step == 0 or i == len(pts) - 1]
    return pts


HIST_COLS = ["date", "block", "region", "portfolio", "avail_mw", "hi300_mw", "hi1000_mw", "hi5000_mw", "pivotal_frac"]
RHIST_COLS = ["date", "block", "region", "avg_rrp", "neg_frac", "below0_mw", "floor_mw", "charge_mw", "charge_pos_mw"]


def _read_csv(path, cols) -> pd.DataFrame:
    if path.exists():
        try:
            h = pd.read_csv(path)
            if "block" not in h.columns:   # rows from before the all-day change were evening-only
                h["block"] = "evening"
            h["block"] = h["block"].fillna("evening")
            return h.reindex(columns=cols)
        except Exception:
            pass
    return pd.DataFrame(columns=cols)


def read_history() -> pd.DataFrame:
    return _read_csv(HISTORY_FILE, HIST_COLS)


def read_region_history() -> pd.DataFrame:
    return _read_csv(REGION_HISTORY_FILE, RHIST_COLS)


def baseline_window(h: pd.DataFrame, day: datetime, date_iso: str) -> pd.DataFrame:
    """The last BASELINE_DAYS trading days strictly before this one."""
    h = h[h["date"] < date_iso]
    if not len(h):
        return h
    h = h[pd.to_datetime(h["date"]) >= day - timedelta(days=BASELINE_DAYS * 2)]
    keep = sorted(h["date"].unique())[-BASELINE_DAYS:]
    return h[h["date"].isin(keep)]


def fmt_mw(x: float) -> str:
    return f"{x:,.0f}MW"


def fmt_px(x: float) -> str:
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def portfolio_table(sb: pd.DataFrame, n: int, piv, base: pd.DataFrame) -> pd.DataFrame:
    """Per region/portfolio dispatchable offers averaged per interval over one block, vs baseline."""
    keys = ["REGIONID", "PORTFOLIO"]
    avg = lambda df: df.groupby(keys)["MW"].sum() / max(n, 1)
    disp_only = sb[~sb["FUEL"].isin(VRE_FUELS)]
    port = pd.DataFrame({
        "avail": avg(disp_only),
        "le0": avg(disp_only[disp_only["PRICE"] < 0]),
        "hi300": avg(disp_only[disp_only["PRICE"] >= 300]),
        "hi1000": avg(disp_only[disp_only["PRICE"] >= 1000]),
        "hi5000": avg(disp_only[disp_only["PRICE"] >= 5000]),
        "vre": avg(sb[sb["FUEL"].isin(VRE_FUELS)]),
    }).fillna(0).reset_index()
    port = port[port["avail"] >= MIN_PORTFOLIO_MW]
    port["share300"] = port["hi300"] / port["avail"] * 100
    fuels = (disp_only.groupby(keys)["FUEL"].agg(lambda s: "/".join(sorted(set(s)))).rename("fuels").reset_index())
    port = port.merge(fuels, on=keys, how="left")
    if piv is not None:
        port = port.merge(piv, on=keys, how="left").fillna({"PIVOTAL_N": 0, "MAX_SHORTFALL": 0})
    else:
        port["PIVOTAL_N"], port["MAX_SHORTFALL"] = np.nan, np.nan
    if len(base):
        b = base.groupby(["region", "portfolio"]).agg(
            b_avail=("avail_mw", "mean"), b_hi300=("hi300_mw", "mean"), b_hi1000=("hi1000_mw", "mean"),
            b_hi5000=("hi5000_mw", "mean"), b_piv=("pivotal_frac", "mean"), b_days=("date", "nunique")).reset_index()
        b["b_share300"] = b["b_hi300"] / b["b_avail"].replace(0, np.nan) * 100
        port = port.merge(b, left_on=keys, right_on=["region", "portfolio"], how="left").drop(columns=["region", "portfolio"])
    for c in ["b_share300", "b_hi1000", "b_hi5000", "b_piv"]:
        if c not in port.columns:
            port[c] = np.nan
    port["b_days"] = port["b_days"].fillna(0) if "b_days" in port.columns else 0
    port["delta_share300"] = np.where(port["b_days"] >= MIN_BASELINE_DAYS, port["share300"] - port["b_share300"], np.nan)
    port["pivotal_frac"] = port["PIVOTAL_N"] / max(n, 1)
    return port


def analyse(day_str: str, backfill: bool = False):
    day = datetime.strptime(day_str, "%Y%m%d")
    date_iso = day.strftime("%Y-%m-%d")
    day_df, per, load_day, load_per = load_bids(day_str)
    disp = load_dispatch(day_str)
    if disp is None:
        log("WARNING: Next_Day_Dispatch not found - wind/solar volumes NOT trimmed to real availability")
    regions = load_regions(day_str)
    ids = pd.concat([day_df[["DUID", "PARTICIPANTID"]], load_day[["DUID", "PARTICIPANTID"]]]).drop_duplicates("DUID")
    reg = registry_table(ids, disp)
    stack = build_stack(day_df, per, disp, reg)
    lstack = build_load_stack(load_day, load_per, reg)
    if regions is not None:
        regions["T"] = pd.to_datetime(regions["INTERVAL_DATETIME"], format=TS_FMT)

    # Interconnector limits: every 5-min interval in the evening peak, half-hourly elsewhere (each
    # interval uses the limit at the end of its half-hour). Fetching all 288 DispatchIS files at
    # once gets NEMWeb returning 403s, so the total stays close to the old evening-only load.
    all_times = [pd.Timestamp(t).to_pydatetime() for t in sorted(stack["T"].unique())]
    ev_start, ev_end = day + timedelta(minutes=1020), day + timedelta(minutes=1230)
    src = {t: (t if ev_start < t <= ev_end else t + timedelta(minutes=(-t.minute) % 30)) for t in all_times}
    fetch_times = sorted(set(src.values()))
    impcap = None if backfill else load_ic_limits(fetch_times)
    if impcap is None:
        try:
            impcap = load_ic_limits_archive(day_str, fetch_times)
        except Exception as exc:
            log(f"WARNING: archive interconnector limits failed ({exc})")
    if impcap is None:
        log("WARNING: no interconnector limits - cushion and pivotal tests skipped")
    else:
        m = pd.DataFrame({"INTERVAL_DATETIME": [t.strftime(TS_FMT) for t in src],
                          "SRC": [v.strftime(TS_FMT) for v in src.values()]})
        impcap = (m.merge(impcap.rename(columns={"INTERVAL_DATETIME": "SRC"}), on="SRC", how="inner")
                  .drop(columns="SRC"))

    hist = read_history()
    hist = hist[hist["date"] != date_iso]
    base = baseline_window(hist, day, date_iso)
    rhist = read_region_history()
    rhist = rhist[rhist["date"] != date_iso]
    rbase = baseline_window(rhist, day, date_iso)

    blocks, reads, new_hist, new_rhist = {}, [], [], []
    empty = pd.DataFrame()
    for key, label, a, b in BLOCKS:
        sb = stack[block_mask(stack["T"], day, a, b)]
        n = int(sb["T"].nunique())
        if n == 0:
            continue
        rb = regions[block_mask(regions["T"], day, a, b)] if regions is not None else None
        lb = lstack[block_mask(lstack["T"], day, a, b)] if len(lstack) else lstack
        cushion, piv = pivotal_and_cushion(sb, rb, impcap)
        port = portfolio_table(sb, n, piv, base[base["block"] == key] if len(base) else base)
        h = port[["REGIONID", "PORTFOLIO", "avail", "hi300", "hi1000", "hi5000", "pivotal_frac"]].rename(columns={
            "REGIONID": "region", "PORTFOLIO": "portfolio", "avail": "avail_mw", "hi300": "hi300_mw",
            "hi1000": "hi1000_mw", "hi5000": "hi5000_mw"})
        h.insert(0, "block", key)
        h.insert(0, "date", date_iso)
        new_hist.append(h)
        focus = "trough" if key in TROUGH_BLOCKS else "peak"

        out_regions = {}
        for region in REGIONS:
            s_pk = sb[sb["REGIONID"] == region]
            if s_pk.empty:
                continue
            rp = rb[rb["REGIONID"] == region] if rb is not None else empty
            if len(rp):
                row = rp.loc[rp["RRP"].idxmin()] if focus == "trough" else rp.loc[rp["TOTALDEMAND"].idxmax()]
                t_f = row["T"]
            else:
                row, t_f = None, s_pk["T"].max()

            by_fuel = (s_pk.groupby(["FUEL", "BUCKET"], observed=False)["MW"].sum() / n).unstack(fill_value=0)
            by_fuel = by_fuel.reindex(columns=BUCKET_LABELS, fill_value=0)
            by_fuel = by_fuel[by_fuel.sum(axis=1) >= 20].round(0)

            p = port[port["REGIONID"] == region].sort_values("hi300", ascending=False)
            ptable = [{
                "portfolio": r.PORTFOLIO, "fuels": r.fuels if isinstance(r.fuels, str) else "",
                "avail": round(r.avail), "le0": round(r.le0), "hi300": round(r.hi300), "hi1000": round(r.hi1000),
                "hi5000": round(r.hi5000), "share300": round(r.share300, 1),
                "delta": None if pd.isna(r.delta_share300) else round(r.delta_share300, 1),
                "pivotal": None if pd.isna(r.PIVOTAL_N) else int(r.PIVOTAL_N),
                "shortfall": None if pd.isna(r.MAX_SHORTFALL) else round(r.MAX_SHORTFALL),
                "vre": round(r.vre),
                "d_hi5000": None if not (r.b_days >= MIN_BASELINE_DAYS) or pd.isna(r.b_hi5000) else round(r.hi5000 - r.b_hi5000),
                "usually_pivotal": bool(r.b_days >= MIN_BASELINE_DAYS and r.b_piv >= 0.7) if not pd.isna(r.b_piv) else False,
            } for r in p.itertuples()]

            cur = s_pk[s_pk["T"] == t_f]
            rc = cushion[cushion["REGIONID"] == region] if cushion is not None else empty
            cush_f = rc[rc["T"] == t_f].iloc[0] if len(rc) and (rc["T"] == t_f).any() else None
            tight = rc.loc[rc["CHEAP_HEADROOM"].idxmin()] if len(rc) else None

            below0 = s_pk[s_pk["PRICE"] < 0]
            floor = s_pk[s_pk["RAW"] <= FLOOR_RAW]
            fuel_mw = lambda df: {f: round(float(v)) for f, v in
                                  (df.groupby("FUEL")["MW"].sum() / n).sort_values(ascending=False).items() if v >= 20}
            lr = lb[lb["REGIONID"] == region] if len(lb) else empty
            charge_mw = float(lr["MW"].sum() / n) if len(lr) else 0.0
            charge_pos = float(lr[lr["PRICE"] >= 0]["MW"].sum() / n) if len(lr) else 0.0
            neg_frac = None if not len(rp) else round(float((rp["RRP"] <= 0).mean()) * 100, 1)

            rd = {
                "focus": focus,
                "peak_time": pd.Timestamp(t_f).strftime("%H:%M"),
                "peak_demand": None if row is None else round(float(row["TOTALDEMAND"])),
                "peak_rrp": None if row is None else round(float(row["RRP"]), 2),
                "peak_dispgen": None if row is None else round(float(row["DISPATCHABLEGENERATION"])),
                "peak_import_cap": None if cush_f is None else round(float(cush_f["IMPORTCAP"])),
                "cushion_pct": None if cush_f is None else round(float(cush_f["CUSHION_PCT"]), 1),
                "cheap_headroom": None if cush_f is None else round(float(cush_f["CHEAP_HEADROOM"])),
                "min_cushion_pct": None if not len(rc) else round(float(rc["CUSHION_PCT"].min()), 1),
                "min_cheap_headroom": None if tight is None else round(float(tight["CHEAP_HEADROOM"])),
                "tightest_time": None if tight is None else pd.Timestamp(tight["T"]).strftime("%H:%M"),
                "max_demand": None if not len(rp) else round(float(rp["TOTALDEMAND"].max())),
                "max_rrp_peak": None if not len(rp) else round(float(rp["RRP"].max()), 2),
                "avg_rrp_peak": None if not len(rp) else round(float(rp["RRP"].mean()), 2),
                "min_rrp": None if not len(rp) else round(float(rp["RRP"].min()), 2),
                "neg_frac": neg_frac,
                "below0_mw": round(float(below0["MW"].sum() / n)),
                "floor_mw": round(float(floor["MW"].sum() / n)),
                "below0_by_fuel": fuel_mw(below0),
                "floor_by_fuel": fuel_mw(floor),
                "charge_mw": round(charge_mw), "charge_pos_mw": round(charge_pos),
                "curve": curve_points(cur, 400 if key == "evening" else 250),
                "by_fuel": {"labels": BUCKET_LABELS, "rows": {f: [float(v) for v in row_] for f, row_ in by_fuel.iterrows()}},
                "portfolios": ptable,
            }
            out_regions[region] = rd
            rbb = rbase[(rbase["block"] == key) & (rbase["region"] == region)] if len(rbase) else rbase
            reads += region_reads(region, rd, ptable, n, key) + negative_reads(region, rd, key, rbb)
            new_rhist.append({"date": date_iso, "block": key, "region": region, "avg_rrp": rd["avg_rrp_peak"],
                              "neg_frac": neg_frac, "below0_mw": rd["below0_mw"], "floor_mw": rd["floor_mw"],
                              "charge_mw": rd["charge_mw"], "charge_pos_mw": rd["charge_pos_mw"]})
        blocks[key] = {"label": label, "window": f"{hm(a)}-{hm(b)}", "n": n, "regions": out_regions}

    # Whole-day spot vs stack-implied series per region (a sanity check on the stack).
    day_out = {}
    if regions is not None:
        for region in REGIONS:
            impl = implied_price(stack[stack["REGIONID"] == region], regions, region)
            if not impl:
                continue
            within = [abs(a - b) <= max(20, 0.2 * abs(b)) for _, a, b in impl if not math.isnan(a)]
            day_out[region] = {
                "series": [{"t": t.strftime("%H:%M"), "impl": None if math.isnan(a) else round(a, 1), "rrp": round(b, 1)}
                           for t, a, b in impl],
                "stack_fit_pct": round(100 * sum(within) / len(within)) if within else None,
            }

    keep_from = (day - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    hist = pd.concat([hist] + new_hist, ignore_index=True)
    hist = hist[hist["date"] >= keep_from].reindex(columns=HIST_COLS).round(1)
    rhist = pd.concat([rhist, pd.DataFrame(new_rhist, columns=RHIST_COLS)], ignore_index=True)
    rhist = rhist[rhist["date"] >= keep_from].reindex(columns=RHIST_COLS).round(1)

    ev = blocks.get("evening", {})
    return {
        "trading_day": date_iso, "peak_window": f"{PEAK_START}-{PEAK_END}", "n_peak": ev.get("n", 0),
        "availability_adjusted": disp is not None, "has_ic": impcap is not None,
        "blocks": blocks, "block_order": [k for k, *_ in BLOCKS if k in blocks],
        "regions": ev.get("regions", {}), "day": day_out, "reads": reads,
    }, hist, rhist, stack


def region_reads(region: str, r: dict, ptable: list[dict], n: int, block: str = "evening") -> list[dict]:
    """Tightness / aggressiveness reads for one block. Evening keeps the original thresholds; the other
    blocks need a bigger move before they're called out, to keep the list readable."""
    name = region.rstrip("1")
    lbl = BLOCK_LABEL[block].lower()
    ev = block == "evening"
    out = []
    add = lambda level, text: out.append({"region": region, "block": block, "level": level, "text": text})
    ch, tt = r.get("min_cheap_headroom"), r.get("tightest_time")
    if ch is not None:
        if ch < 0:
            add(3, f"{name} {lbl}: at {tt}, sub-$300 offers plus full import capability fell {fmt_mw(-ch)} short "
                   f"of demand - the region needed $300+ capacity to clear.")
        elif ch < min(500, 0.15 * (r.get("max_demand") or r.get("peak_demand") or 3500)):
            add(2, f"{name} {lbl}: only {fmt_mw(ch)} of sub-$300 supply (incl. imports) above demand at {tt} - "
                   f"one unit trip or a warmer night moves it into $300+ bands.")
    piv = [p for p in ptable if p["pivotal"]]
    for p in sorted(piv, key=lambda x: -x["pivotal"])[:3]:
        if p["usually_pivotal"]:   # structural (e.g. Hydro Tas in TAS) - keep it, but don't shout
            if ev:
                add(1, f"{name}: {p['portfolio']} pivotal in {p['pivotal']}/{n} {lbl} intervals, as it usually is "
                       f"- {fmt_mw(p['hi300'])} offered at $300+.")
            continue
        add(3 if p["pivotal"] >= n / 2 else 2,
            f"{name} {lbl}: {p['portfolio']} was pivotal in {p['pivotal']}/{n} intervals (demand couldn't be met "
            f"without it, up to {fmt_mw(p['shortfall'])} short) - it had {fmt_mw(p['hi300'])} offered at $300+.")
    thr = 10 if ev else 15
    for p in ptable:
        if p["delta"] is not None and abs(p["delta"]) >= thr and p["avail"] >= (200 if ev else 300):
            direction = "more" if p["delta"] > 0 else "less"
            add(2 if p["delta"] > 0 else 1,
                f"{name} {lbl}: {p['portfolio']} priced {abs(p['delta']):.0f}pp {direction} of its capacity at $300+ "
                f"than its {BASELINE_DAYS}-day norm ({p['share300']:.0f}% of {fmt_mw(p['avail'])}).")
    # $5k+ volume: once a baseline exists, only call out a material change vs the portfolio's norm
    # (Snowy parking hydro at the cap is normal; Snowy adding 500MW there is not).
    moved = [p for p in ptable if p["d_hi5000"] is not None and abs(p["d_hi5000"]) >= (150 if ev else 250)]
    for p in sorted(moved, key=lambda x: -abs(x["d_hi5000"]))[:2]:
        verb = "added" if p["d_hi5000"] > 0 else "pulled"
        add(2 if p["d_hi5000"] > 0 else 1,
            f"{name} {lbl}: {p['portfolio']} {verb} {fmt_mw(abs(p['d_hi5000']))} {'to' if p['d_hi5000'] > 0 else 'from'} "
            f"$5,000+ bands vs its norm (now {fmt_mw(p['hi5000'])}).")
    if ev and all(p["d_hi5000"] is None for p in ptable):
        top = [p for p in ptable if p["hi5000"] >= 100]
        for p in sorted(top, key=lambda x: -x["hi5000"])[:2]:
            add(1, f"{name}: {p['portfolio']} had {fmt_mw(p['hi5000'])} sitting at $5,000+ through the evening "
                   f"({p['share300']:.0f}% of its dispatchable capacity at $300+). Baseline still building.")
    return out


def negative_reads(region: str, r: dict, block: str, rb: pd.DataFrame) -> list[dict]:
    """The bottom of the stack: how often price went to/below zero, how much volume sits below $0
    and at the floor, who it is, and storage bidding to soak it up."""
    name = region.rstrip("1")
    lbl = BLOCK_LABEL[block].lower()
    nf = r.get("neg_frac")
    out = []
    if nf is None:
        return out
    add = lambda level, text: out.append({"region": region, "block": block, "level": level, "text": text})
    nb = rb["date"].nunique() if len(rb) else 0
    bn = rb["neg_frac"].mean() if nb >= MIN_BASELINE_DAYS else math.nan
    norm = "" if pd.isna(bn) else f", vs a {bn:.0f}% norm"
    if nf >= 10:
        floor_bits = ", ".join(f"{f} {fmt_mw(v)}" for f, v in list(r["floor_by_fuel"].items())[:3])
        coal = sum(v for f, v in r["below0_by_fuel"].items() if "Coal" in f)
        text = (f"{name} {lbl}: spot at or below $0 in {nf:.0f}% of intervals{norm} (low {fmt_px(r['min_rrp'])}). "
                f"{fmt_mw(r['below0_mw'])} offered below $0, {fmt_mw(r['floor_mw'])} of it at the -$1,000 floor"
                + (f" ({floor_bits})" if floor_bits else "") + ".")
        if coal >= 100:
            text += f" Coal has {fmt_mw(coal)} below $0 (min-load protection)."
        if r["charge_mw"] >= 100:
            text += (f" Storage bid to charge {fmt_mw(r['charge_mw'])}, {fmt_mw(r['charge_pos_mw'])} of it "
                     f"even at positive prices.")
        add(1, text)
    if nb >= MIN_BASELINE_DAYS:
        bf = float(rb["floor_mw"].mean())
        d = r["floor_mw"] - bf
        if abs(d) >= max(200, 0.25 * bf):
            more = d > 0
            add(2 if more else 1,
                f"{name} {lbl}: {fmt_mw(abs(d))} {'more' if more else 'less'} offered at the -$1,000 floor than the "
                f"{BASELINE_DAYS}-day norm ({fmt_mw(r['floor_mw'])} vs {fmt_mw(bf)}) - "
                + ("more volume runs at any price, so surplus intervals clear deeper negative."
                   if more else "less must-run volume, so negatives should be shallower or rarer."))
        if nf < 10 and not pd.isna(bn) and bn - nf >= 15:
            add(1, f"{name} {lbl}: at or below $0 in only {nf:.0f}% of intervals vs a {bn:.0f}% norm "
                   f"(low {fmt_px(r['min_rrp'])}).")
        bc = rb["charge_mw"].mean()
        if not pd.isna(bc) and abs(r["charge_mw"] - bc) >= max(150, 0.3 * bc) and block in TROUGH_BLOCKS:
            add(1, f"{name} {lbl}: storage bid to charge {fmt_mw(r['charge_mw'])} vs a {fmt_mw(bc)} norm.")
    return out


# ---------------------------------------------------------------------------
# Forward look - predispatch demand vs. yesterday's same-time offers
# ---------------------------------------------------------------------------

def forward_look(stack: pd.DataFrame, trading_day: datetime) -> list[dict]:
    """For each upcoming block in predispatch, the highest-demand half-hour set against the offers each
    portfolio made for the same half-hour yesterday."""
    pd_data = load_predispatch()
    if pd_data is None:
        return []
    reg, ic = pd_data
    now = datetime.now(nw.NEM_TZ).replace(tzinfo=None)
    reg = reg[reg["PERIOD"] > now].copy()
    reg["BLOCK"] = reg["PERIOD"].map(tod_block)
    reg["TDATE"] = (reg["PERIOD"] - timedelta(hours=4, minutes=1)).dt.date
    out = []
    # yesterday's offers keyed by time of day (half-hour ending), averaged over the 5-min intervals
    st = stack.copy()
    st["HH"] = (st["T"] + pd.to_timedelta((-st["T"].dt.minute % 30), unit="m")).dt.strftime("%H:%M")
    n_per_hh = st.groupby("HH")["T"].nunique()
    order = {k: i for i, (k, *_) in enumerate(BLOCKS)}
    for region in REGIONS:
        rr = reg[reg["REGIONID"] == region]
        if rr.empty:
            continue
        for (td, blk), g in rr.groupby(["TDATE", "BLOCK"]):
            row = g.loc[g["TOTALDEMAND"].idxmax()]
            hh = row["PERIOD"].strftime("%H:%M")
            s = st[(st["REGIONID"] == region) & (st["HH"] == hh)]
            if s.empty:
                continue
            n = n_per_hh.get(hh, 1)
            avail = s["MW"].sum() / n
            avail300 = s[s["PRICE"] < 300]["MW"].sum() / n
            vre = s[s["FUEL"].isin(VRE_FUELS)]["MW"].sum() / n
            icr = ic[(ic["REGIONID"] == region) & (ic["PERIOD"] == row["PERIOD"])]
            impcap = float(icr["IMPORTCAP"].iloc[0]) if len(icr) else math.nan
            demand = float(row["TOTALDEMAND"])
            own = s.groupby("PORTFOLIO")["MW"].sum() / n
            pivotal = []
            if not math.isnan(impcap):
                for o, mw in own.sort_values(ascending=False).items():
                    if avail - mw + impcap < demand:
                        pivotal.append({"portfolio": o, "short": round(demand - (avail - mw + impcap))})
            out.append({
                "region": region, "block": blk, "block_label": BLOCK_LABEL.get(blk, blk),
                "date": row["PERIOD"].strftime("%a %d %b"), "time": hh, "_sort": (td, order.get(blk, 9)),
                "demand": round(demand), "pd_rrp": round(float(row["RRP"]), 2), "pd_min_rrp": round(float(g["RRP"].min()), 2),
                "avail": round(avail), "vre": round(vre), "import_cap": None if math.isnan(impcap) else round(impcap),
                "cushion_pct": None if math.isnan(impcap) else round((avail + impcap - demand) / demand * 100, 1),
                "cheap_headroom": None if math.isnan(impcap) else round(avail300 + impcap - demand),
                "pivotal": pivotal[:4],
            })
    out.sort(key=lambda f: (f["region"], f["_sort"]))
    for f in out:
        f.pop("_sort")
    return out


def forward_reads(fwd: list[dict], usual: set) -> list[dict]:
    out = []
    for f in fwd:
        key = lambda p: (f["block"], f["region"], p["portfolio"])
        f["pivotal"] = [p for p in f["pivotal"] if key(p) not in usual] + \
                       [dict(p, usual=True) for p in f["pivotal"] if key(p) in usual]
        unusual = [p for p in f["pivotal"] if not p.get("usual")]
        name = f["region"].rstrip("1")
        where = f"{name} {f['date']} {f['time']} ({f['block_label'].lower()})"
        # Midday/overnight headroom depends on tomorrow's wind and solar, which yesterday's offers
        # don't know - only flag it there when predispatch itself is pricing scarcity.
        trough_ok = f["block"] not in TROUGH_BLOCKS or f["pd_rrp"] >= 150
        if f["cheap_headroom"] is not None and f["cheap_headroom"] < 300 and trough_ok:
            who = ", ".join(p["portfolio"] for p in unusual[:2])
            out.append({"region": f["region"], "block": f["block"], "level": 3 if f["cheap_headroom"] < 0 else 2,
                        "forward": True, "text":
                f"{where}: forecast demand {fmt_mw(f['demand'])} vs yesterday's offers leaves "
                f"{fmt_mw(f['cheap_headroom'])} of sub-$300 headroom (predispatch {fmt_px(f['pd_rrp'])})"
                + (f"; pivotal: {who}." if who else ".")})
        elif unusual:
            who = ", ".join(p["portfolio"] for p in unusual[:2])
            out.append({"region": f["region"], "block": f["block"], "level": 2, "forward": True, "text":
                f"{where}: {who} would be pivotal at forecast demand {fmt_mw(f['demand'])} "
                f"if offers match yesterday's (predispatch {fmt_px(f['pd_rrp'])})."})
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_dashboard(result: dict) -> Path:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    data_dir = DOCS_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    payload = json.dumps(result, default=str, separators=(",", ":"))
    (data_dir / f"{result['trading_day']}.json").write_text(payload)
    for old in sorted(data_dir.glob("*.json"))[:-30]:
        old.unlink()
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", payload.replace("</", "<\\/"))
    out = DOCS_DIR / "index.html"
    out.write_text(html)
    return out


def ntfy_summary(result: dict) -> str:
    lines = [f"Bid stack {result['trading_day']} (evening {result['peak_window']} NEM)"]
    for region, r in result["regions"].items():
        top = sorted(r["portfolios"], key=lambda p: -p["hi300"])[:1]
        bits = [f"{region.rstrip('1')}: peak {r['peak_time']}"]
        if r["cheap_headroom"] is not None:
            bits.append(f"sub-$300 headroom {r['cheap_headroom']:,}MW")
        if top:
            bits.append(f"most $300+: {top[0]['portfolio']} {top[0]['hi300']:,}MW")
        lines.append(" | ".join(bits))
    day = result.get("blocks", {}).get("daytime", {}).get("regions", {})
    if day:
        lines.append("Daytime 09:00-17:00:")
        for region, r in day.items():
            if r.get("neg_frac") is None:
                continue
            lines.append(f"{region.rstrip('1')}: <=$0 {r['neg_frac']:.0f}% of intervals | "
                         f"{r['below0_mw']:,}MW below $0, {r['floor_mw']:,}MW at floor | charge bids {r['charge_mw']:,}MW")
    ranked = sorted(result["reads"], key=lambda x: -x["level"])[:6]
    if ranked:
        lines.append("")
        lines += [f"- {x['text']}" for x in ranked]
    return "\n".join(lines)


def bidstack_topic() -> str | None:
    """Own ntfy topic. Uses ntfy_topics.bidstack if the NTFY_TOPICS_JSON secret has it; otherwise
    derives one from the recap topic's private suffix (nem-recap-XXXX -> nem-bidstack-XXXX), so
    the real topic string never has to be committed to this public repo."""
    topics = nw.CONFIG.get("ntfy_topics", {})
    if topics.get("bidstack"):
        return topics["bidstack"]
    recap = topics.get("recap") or ""
    if "recap" in recap and not recap.startswith("CHANGE_ME"):
        return recap.replace("recap", "bidstack", 1)
    return None


def push_latest() -> None:
    """Push the latest processed day's dashboard as an .html attachment, once per trading day."""
    state = nw.read_state(STATE_FILE, default={}) or {}
    day = state.get("last_trading_day")
    page = DOCS_DIR / "index.html"
    data_path = DOCS_DIR / "data" / f"{day}.json"
    if not day or not page.exists() or not data_path.exists():
        log("nothing processed yet - no push")
        return
    if state.get("last_pushed_day") == day:
        log(f"{day} already pushed - skipping")
        return
    topic = bidstack_topic()
    if not topic:
        log("no bid stack ntfy topic configured - skipping push")
        return
    data = json.loads(data_path.read_text())
    ranked = sorted(data.get("reads", []), key=lambda x: -x.get("level", 0))
    headline = ranked[0]["text"] if ranked else "No standout bidding moves."
    ok = nw.push_ntfy_attachment(topic, f"bidstack-{day}.html", page.read_text(),
                                 short_message=f"Tap to open. {headline}"[:300].replace("\n", " "),
                                 title=f"Bid stack - {day}", tags=["bar_chart"])
    if ok:
        state["last_pushed_day"] = day
        nw.write_state(STATE_FILE, state)


def main() -> None:
    if "--push-only" in sys.argv:
        push_latest()
        return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    no_push = "--no-push" in sys.argv
    allow_partial = "--allow-partial" in sys.argv
    now = datetime.now(nw.NEM_TZ).replace(tzinfo=None)
    day = args[0] if args else (now - timedelta(days=1)).strftime("%Y%m%d")
    day_iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"

    if "--backfill" in sys.argv:
        # Seed the per-portfolio baseline from the ~60 days AEMO keeps in CURRENT, oldest first.
        n = int(args[0]) if args else BASELINE_DAYS
        for k in range(n, 0, -1):
            d = (now - timedelta(days=k + 1)).strftime("%Y%m%d")
            try:
                _, hist, rhist, _ = analyse(d, backfill=True)
                hist.to_csv(HISTORY_FILE, index=False)
                rhist.to_csv(REGION_HISTORY_FILE, index=False)
                log(f"backfilled {d}")
            except Exception as exc:
                log(f"backfill {d} skipped: {exc}")
        return

    state = nw.read_state(STATE_FILE, default={}) or {}
    if state.get("last_trading_day") == day_iso and not force:
        log(f"already processed {day_iso} - skipping")
        return

    try:
        result, hist, rhist, stack = analyse(day)
    except FileNotFoundError as exc:
        log(f"{exc} - will retry on the next trigger")
        return

    if not result["availability_adjusted"] and not allow_partial and now.hour < 9:
        log("Next_Day_Dispatch missing - waiting for a later run rather than publishing untrimmed volumes")
        return

    fwd = forward_look(stack, datetime.strptime(day, "%Y%m%d"))
    result["forward"] = fwd
    usual = {(blk, rg, p["portfolio"]) for blk, B in result["blocks"].items()
             for rg, r in B["regions"].items() for p in r["portfolios"] if p["usually_pivotal"]}
    result["reads"] = forward_reads(fwd, usual) + result["reads"]
    result["generated"] = now.strftime("%Y-%m-%d %H:%M NEM")
    if not result["availability_adjusted"]:
        result["reads"].insert(0, {"region": "NEM", "level": 1, "text":
            "Next-day dispatch file wasn't available - wind/solar volumes are as bid, not trimmed to real availability."})

    out = write_dashboard(result)
    hist.to_csv(HISTORY_FILE, index=False)
    rhist.to_csv(REGION_HISTORY_FILE, index=False)
    log(f"dashboard written: {out}")

    print(ntfy_summary(result))
    # No push here: the dashboard is pushed alongside the morning recap (--push-only, run from
    # recap.yml) to its own ntfy topic.
    prev = state.get("last_pushed_day")
    nw.write_state(STATE_FILE, {"last_trading_day": day_iso, "generated": result["generated"],
                                **({"last_pushed_day": prev} if prev else {})})


if __name__ == "__main__":
    main()
