"""
Bid Stack Analysis - daily, next-morning

What it answers
---------------
  * Who is pricing aggressively (capacity offered at $300+, $1,000+, $5,000+) and
    whether that is unusual for them (vs their own trailing baseline).
  * How much cheap supply is left above demand at the evening peak (supply cushion).
  * Who is pivotal - i.e. the region cannot meet demand without them, so they can
    set price if they choose.
  * A forward read for the next evening peaks: predispatch forecast demand set
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
PREDISPATCH_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Predispatch_Reports/"

REGIONS = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]
PEAK_START, PEAK_END = "17:00", "20:30"   # interval-ending times, NEM time, inclusive of end
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
ENERGY_GEN = {"BIDTYPE": {"ENERGY"}, "DIRECTION": {"GEN", ""}}


def load_bids(day: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    url = find_file(BIDMOVE_URL, rf"^PUBLIC_BIDMOVE_COMPLETE_{day}_\d+\.zip$")
    if not url:
        raise FileNotFoundError(f"Bidmove_Complete for {day} not published yet")
    log(f"downloading {url.rsplit('/', 1)[-1]}")
    t = stream_tables(nw.download_bytes(url), {
        "BIDDAYOFFER_D": (["DUID", "PARTICIPANTID"] + PRICES, ENERGY_GEN),
        "BIDPEROFFER_D": (["DUID", "INTERVAL_DATETIME", "MAXAVAIL", "FIXEDLOAD"] + BANDS, ENERGY_GEN),
    })
    day_df = num(t["BIDDAYOFFER_D"].drop_duplicates("DUID", keep="last"), PRICES)
    per = num(t["BIDPEROFFER_D"], ["MAXAVAIL", "FIXEDLOAD"] + BANDS)
    log(f"bids: {len(day_df)} DUIDs with energy price bands, {len(per)} per-interval volume rows")
    return day_df, per


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

    with ThreadPoolExecutor(8) as pool:
        frames = [f for f in pool.map(fetch, picks) if f is not None and len(f)]
    if not frames:
        return None
    ic = num(pd.concat(frames), ["IMPORTLIMIT", "EXPORTLIMIT"])
    log(f"interconnector limits: {len(picks)} peak intervals")
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

CP_REGION = {"N": "NSW1", "Q": "QLD1", "V": "VIC1", "S": "SA1", "T": "TAS1"}


def infer_fuel(duid: str) -> str:
    """Last-resort fuel guess from AEMO's DUID naming habits, only for units missing from registry/."""
    d = duid.upper()
    if d.startswith("DR"):                      # wholesale demand response units (DRXN..., DRVI...)
        return "Demand response"
    if re.search(r"BES|BAT|BESS|BS\d|^ERB|RB\d$|BA\d", d):
        return "Battery"
    if re.search(r"PHG|PSH|HYD", d):
        return "Hydro"
    if re.search(r"SF|SOL|PV\d", d):
        return "Solar"
    if re.search(r"WF|WND|WIND", d):
        return "Wind"
    return "Other"


def registry_table(day_df: pd.DataFrame, disp: pd.DataFrame | None) -> pd.DataFrame:
    reg = nw.load_registry()
    base = pd.DataFrame({"DUID": day_df["DUID"], "PARTICIPANTID": day_df["PARTICIPANTID"]})
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
    # Units newer than the registry files: region from the connection point (AEMO TNI codes start
    # with the region letter), fuel guessed from the DUID, portfolio = trading participant.
    base["INFERRED"] = base["REGIONID"].isna() | ~base["REGIONID"].isin(REGIONS)
    if disp is not None and "CONNECTIONPOINTID" in disp.columns:
        cp = disp.drop_duplicates("DUID").set_index("DUID")["CONNECTIONPOINTID"].astype(str).str[:1].map(CP_REGION)
        base.loc[base["INFERRED"], "REGIONID"] = base.loc[base["INFERRED"], "DUID"].map(cp)
    base.loc[base["INFERRED"] & base["FUEL"].isna(), "FUEL"] = base.loc[base["INFERRED"] & base["FUEL"].isna(), "DUID"].map(infer_fuel)
    base = base[~base["DUID"].str.startswith("DG_")]   # AEMO dummy generators, not real supply
    if reg.owner_capacity is not None and "Owner" in reg.owner_capacity.columns:
        base = base.merge(reg.owner_capacity[["DUID", "Owner"]], on="DUID", how="left")
    else:
        base["Owner"] = np.nan
    # Portfolio = trading brand (AGL, Origin Energy, CS Energy...), the level at which bidding
    # strategy is actually set. Legal-entity Owner and raw PARTICIPANTID are fallbacks only.
    base["PORTFOLIO"] = base["PORTFOLIO"].fillna(base["Owner"]).fillna(base["PARTICIPANTID"])
    base["FUEL"] = base["FUEL"].fillna("Other")
    base["STATIONNAME"] = base["STATIONNAME"].fillna(base["DUID"])
    base["TLF"] = pd.to_numeric(base["TransmissionLossFactor"], errors="coerce").fillna(1.0)
    base.loc[(base["TLF"] < 0.5) | (base["TLF"] > 1.5), "TLF"] = 1.0
    inferred = base[base["INFERRED"] & base["REGIONID"].isin(REGIONS)]
    if len(inferred):
        log(f"NOTE: {len(inferred)} DUIDs missing from registry/ - region from connection point, fuel guessed: "
            + ", ".join(f"{r.DUID}({r.FUEL})" for r in inferred.itertuples()))
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
    prices = df[PRICES].to_numpy(dtype=float) / tlf[:, None]   # refer to regional reference node

    n = len(df)
    parts = []
    meta = df[["DUID", "INTERVAL_DATETIME", "REGIONID", "PORTFOLIO", "FUEL"]]
    for k in range(10):
        m = adj[:, k] > 0
        p = meta[m].copy()
        p["PRICE"] = prices[m, k]
        p["MW"] = adj[m, k]
        parts.append(p)
    if is_fixed.any():
        p = meta[is_fixed].copy()
        p["PRICE"] = -1000.0
        p["MW"] = np.minimum(fixed[is_fixed], cap[is_fixed])
        parts.append(p)
    stack = pd.concat(parts, ignore_index=True)
    stack = stack[stack["REGIONID"].isin(REGIONS)]
    stack["T"] = pd.to_datetime(stack["INTERVAL_DATETIME"], format=TS_FMT)
    stack["BUCKET"] = pd.cut(stack["PRICE"], BUCKET_EDGES, labels=BUCKET_LABELS, right=False)
    unknown = set(df.loc[~df["REGIONID"].isin(REGIONS), "DUID"])
    if unknown:
        log(f"NOTE: {len(unknown)} DUIDs have no region in registry/ and were excluded: {sorted(unknown)[:12]}")
    log(f"stack: {len(stack)} unit-interval-band rows from {n} unit-intervals")
    return stack


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def peak_mask(times: pd.Series, day: datetime) -> pd.Series:
    start = datetime.strptime(f"{day:%Y-%m-%d} {PEAK_START}", "%Y-%m-%d %H:%M")
    end = datetime.strptime(f"{day:%Y-%m-%d} {PEAK_END}", "%Y-%m-%d %H:%M")
    return (times > start) & (times <= end)


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


def read_history() -> pd.DataFrame:
    cols = ["date", "region", "portfolio", "avail_mw", "hi300_mw", "hi1000_mw", "hi5000_mw", "pivotal_frac"]
    if HISTORY_FILE.exists():
        try:
            return pd.read_csv(HISTORY_FILE).reindex(columns=cols)
        except Exception:
            pass
    return pd.DataFrame(columns=["date", "region", "portfolio", "avail_mw", "hi300_mw", "hi1000_mw", "hi5000_mw",
                                 "pivotal_frac"])


def fmt_mw(x: float) -> str:
    return f"{x:,.0f}MW"


def analyse(day_str: str):
    day = datetime.strptime(day_str, "%Y%m%d")
    day_df, per = load_bids(day_str)
    disp = load_dispatch(day_str)
    if disp is None:
        log("WARNING: Next_Day_Dispatch not found - wind/solar volumes NOT trimmed to real availability")
    regions = load_regions(day_str)
    reg = registry_table(day_df, disp)
    stack = build_stack(day_df, per, disp, reg)
    if regions is not None:
        regions["T"] = pd.to_datetime(regions["INTERVAL_DATETIME"], format=TS_FMT)

    pk = peak_mask(stack["T"], day)
    stack_peak = stack[pk]
    peak_times = sorted(stack_peak["T"].unique())
    n_peak = len(peak_times)
    regions_peak = regions[peak_mask(regions["T"], day)] if regions is not None else None

    impcap = load_ic_limits([pd.Timestamp(t).to_pydatetime() for t in peak_times])
    cushion, piv = pivotal_and_cushion(stack_peak, regions_peak, impcap)

    # ---- portfolio aggressiveness (evening peak, averaged per interval) ----
    def per_interval_avg(df, by):
        return df.groupby(by)["MW"].sum() / max(n_peak, 1)

    keys = ["REGIONID", "PORTFOLIO"]
    disp_only = stack_peak[~stack_peak["FUEL"].isin(VRE_FUELS)]
    port = pd.DataFrame({
        "avail": per_interval_avg(disp_only, keys),
        "le0": per_interval_avg(disp_only[disp_only["PRICE"] < 0], keys),
        "hi300": per_interval_avg(disp_only[disp_only["PRICE"] >= 300], keys),
        "hi1000": per_interval_avg(disp_only[disp_only["PRICE"] >= 1000], keys),
        "hi5000": per_interval_avg(disp_only[disp_only["PRICE"] >= 5000], keys),
        "vre": per_interval_avg(stack_peak[stack_peak["FUEL"].isin(VRE_FUELS)], keys),
    }).fillna(0).reset_index()
    port = port[port["avail"] >= MIN_PORTFOLIO_MW]
    port["share300"] = port["hi300"] / port["avail"] * 100
    fuels = (stack_peak[~stack_peak["FUEL"].isin(VRE_FUELS)].groupby(keys)["FUEL"]
             .agg(lambda s: "/".join(sorted(set(s)))).rename("fuels").reset_index())
    port = port.merge(fuels, on=keys, how="left")
    if piv is not None:
        port = port.merge(piv, left_on=keys, right_on=keys, how="left").fillna({"PIVOTAL_N": 0, "MAX_SHORTFALL": 0})
    else:
        port["PIVOTAL_N"], port["MAX_SHORTFALL"] = np.nan, np.nan

    hist = read_history()
    date_iso = day.strftime("%Y-%m-%d")
    hist = hist[hist["date"] != date_iso]
    base = hist.copy()
    if len(base):
        base["date_dt"] = pd.to_datetime(base["date"])
        base = base[base["date_dt"] >= day - timedelta(days=BASELINE_DAYS * 2)]
        last_dates = sorted(base["date"].unique())[-BASELINE_DAYS:]
        base = base[base["date"].isin(last_dates)]
        b = base.groupby(["region", "portfolio"]).agg(
            b_avail=("avail_mw", "mean"), b_hi300=("hi300_mw", "mean"), b_hi1000=("hi1000_mw", "mean"),
            b_piv=("pivotal_frac", "mean"), b_days=("date", "nunique")).reset_index()
        b["b_share300"] = b["b_hi300"] / b["b_avail"].replace(0, np.nan) * 100
        port = port.merge(b, left_on=keys, right_on=["region", "portfolio"], how="left").drop(columns=["region", "portfolio"])
    else:
        port["b_share300"], port["b_days"], port["b_hi1000"], port["b_piv"] = np.nan, 0, np.nan, np.nan
    if "b_piv" not in port.columns:
        port["b_piv"] = np.nan
    port["delta_share300"] = np.where(port["b_days"].fillna(0) >= MIN_BASELINE_DAYS,
                                      port["share300"] - port["b_share300"], np.nan)

    port["pivotal_frac"] = port["PIVOTAL_N"] / max(n_peak, 1)
    new_hist = port[keys + ["avail", "hi300", "hi1000", "hi5000", "pivotal_frac"]].rename(columns={
        "REGIONID": "region", "PORTFOLIO": "portfolio", "avail": "avail_mw", "hi300": "hi300_mw",
        "hi1000": "hi1000_mw", "hi5000": "hi5000_mw"})
    new_hist.insert(0, "date", date_iso)
    hist = pd.concat([hist, new_hist], ignore_index=True)
    keep_from = (day - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    hist = hist[hist["date"] >= keep_from].round(1)

    # ---- per-region outputs ----
    out_regions = {}
    reads = []
    for region in REGIONS:
        s_all = stack[stack["REGIONID"] == region]
        s_pk = stack_peak[stack_peak["REGIONID"] == region]
        if s_pk.empty:
            continue
        rp = regions_peak[regions_peak["REGIONID"] == region] if regions_peak is not None else pd.DataFrame()
        if len(rp):
            peak_row = rp.loc[rp["TOTALDEMAND"].idxmax()]
            t_peak = peak_row["T"]
        else:
            peak_row, t_peak = None, s_pk["T"].max()

        by_fuel = (s_pk.groupby(["FUEL", "BUCKET"], observed=False)["MW"].sum() / n_peak).unstack(fill_value=0)
        by_fuel = by_fuel.reindex(columns=BUCKET_LABELS, fill_value=0)
        by_fuel = by_fuel[by_fuel.sum(axis=1) > 1].round(0)

        p = port[port["REGIONID"] == region].sort_values("hi300", ascending=False)
        ptable = [{
            "portfolio": r.PORTFOLIO, "fuels": r.fuels if isinstance(r.fuels, str) else "",
            "avail": round(r.avail), "le0": round(r.le0), "hi300": round(r.hi300), "hi1000": round(r.hi1000),
            "hi5000": round(r.hi5000), "share300": round(r.share300, 1),
            "delta": None if pd.isna(r.delta_share300) else round(r.delta_share300, 1),
            "pivotal": None if pd.isna(r.PIVOTAL_N) else int(r.PIVOTAL_N),
            "shortfall": None if pd.isna(r.MAX_SHORTFALL) else round(r.MAX_SHORTFALL),
            "vre": round(r.vre),
            "usually_pivotal": bool(r.b_days >= MIN_BASELINE_DAYS and r.b_piv >= 0.7) if not pd.isna(r.b_piv) else False,
        } for r in p.itertuples()]

        cur = s_pk[s_pk["T"] == t_peak]
        rc = cushion[(cushion["REGIONID"] == region)] if cushion is not None else pd.DataFrame()
        cush_peak = rc[rc["T"] == t_peak].iloc[0] if len(rc) and (rc["T"] == t_peak).any() else None

        impl = implied_price(s_all, regions, region) if regions is not None else []
        within = [abs(a - b) <= max(20, 0.2 * abs(b)) for _, a, b in impl if not math.isnan(a)]
        out_regions[region] = {
            "peak_time": pd.Timestamp(t_peak).strftime("%H:%M"),
            "peak_demand": None if peak_row is None else round(float(peak_row["TOTALDEMAND"])),
            "peak_rrp": None if peak_row is None else round(float(peak_row["RRP"]), 2),
            "peak_dispgen": None if peak_row is None else round(float(peak_row["DISPATCHABLEGENERATION"])),
            "peak_import_cap": None if cush_peak is None else round(float(cush_peak["IMPORTCAP"])),
            "cushion_pct": None if cush_peak is None else round(float(cush_peak["CUSHION_PCT"]), 1),
            "cheap_headroom": None if cush_peak is None else round(float(cush_peak["CHEAP_HEADROOM"])),
            "min_cushion_pct": None if not len(rc) else round(float(rc["CUSHION_PCT"].min()), 1),
            "min_cheap_headroom": None if not len(rc) else round(float(rc["CHEAP_HEADROOM"].min())),
            "max_rrp_peak": None if not len(rp) else round(float(rp["RRP"].max()), 2),
            "avg_rrp_peak": None if not len(rp) else round(float(rp["RRP"].mean()), 2),
            "curve": curve_points(cur),
            "by_fuel": {"labels": BUCKET_LABELS, "rows": {f: [float(v) for v in row] for f, row in by_fuel.iterrows()}},
            "portfolios": ptable,
            "series": [{"t": t.strftime("%H:%M"), "impl": None if math.isnan(a) else round(a, 1), "rrp": round(b, 1)}
                       for t, a, b in impl],
            "stack_fit_pct": round(100 * sum(within) / len(within)) if within else None,
        }
        reads += region_reads(region, out_regions[region], ptable, n_peak)

    return {
        "trading_day": date_iso, "peak_window": f"{PEAK_START}-{PEAK_END}", "n_peak": n_peak,
        "availability_adjusted": disp is not None, "has_ic": impcap is not None,
        "regions": out_regions, "reads": reads,
    }, hist, stack


def region_reads(region: str, r: dict, ptable: list[dict], n_peak: int) -> list[dict]:
    name = region.rstrip("1")
    out = []
    ch = r.get("cheap_headroom")
    if ch is not None:
        if ch < 0:
            out.append({"region": region, "level": 3, "text":
                f"{name}: at the {r['peak_time']} peak, sub-$300 offers plus full import capability fell "
                f"{fmt_mw(-ch)} short of demand - the region needed $300+ capacity to clear."})
        elif ch < 500:
            out.append({"region": region, "level": 2, "text":
                f"{name}: only {fmt_mw(ch)} of sub-$300 supply (incl. imports) above demand at the "
                f"{r['peak_time']} peak - one unit trip or a warmer evening moves it into $300+ bands."})
    piv = [p for p in ptable if p["pivotal"]]
    for p in sorted(piv, key=lambda x: -x["pivotal"])[:3]:
        if p["usually_pivotal"]:   # structural (e.g. Hydro Tas in TAS) - keep it, but don't shout
            out.append({"region": region, "level": 1, "text":
                f"{name}: {p['portfolio']} pivotal in {p['pivotal']}/{n_peak} evening intervals, as it usually is "
                f"- {fmt_mw(p['hi300'])} offered at $300+."})
            continue
        out.append({"region": region, "level": 3 if p["pivotal"] >= n_peak / 2 else 2, "text":
            f"{name}: {p['portfolio']} was pivotal in {p['pivotal']}/{n_peak} evening intervals "
            f"(demand couldn't be met without it, up to {fmt_mw(p['shortfall'])} short) - "
            f"it had {fmt_mw(p['hi300'])} offered at $300+."})
    for p in ptable:
        if p["delta"] is not None and abs(p["delta"]) >= 10 and p["avail"] >= 200:
            direction = "more" if p["delta"] > 0 else "less"
            out.append({"region": region, "level": 2 if p["delta"] > 0 else 1, "text":
                f"{name}: {p['portfolio']} priced {abs(p['delta']):.0f}pp {direction} of its capacity at $300+ "
                f"than its {BASELINE_DAYS}-day norm ({p['share300']:.0f}% of {fmt_mw(p['avail'])})."})
    top = [p for p in ptable if p["hi5000"] >= 100]
    for p in sorted(top, key=lambda x: -x["hi5000"])[:2]:
        out.append({"region": region, "level": 1, "text":
            f"{name}: {p['portfolio']} had {fmt_mw(p['hi5000'])} sitting at $5,000+ through the evening "
            f"({p['share300']:.0f}% of its dispatchable capacity at $300+)."})
    return out


# ---------------------------------------------------------------------------
# Forward look - predispatch demand vs. yesterday's same-time offers
# ---------------------------------------------------------------------------

def forward_look(stack: pd.DataFrame, trading_day: datetime) -> list[dict]:
    pd_data = load_predispatch()
    if pd_data is None:
        return []
    reg, ic = pd_data
    now = datetime.now(nw.NEM_TZ).replace(tzinfo=None)
    reg = reg[reg["PERIOD"] > now]
    out = []
    # yesterday's offers keyed by time of day (half-hour ending), averaged over the 5-min intervals
    st = stack.copy()
    st["HH"] = (st["T"] + pd.to_timedelta((-st["T"].dt.minute % 30), unit="m")).dt.strftime("%H:%M")
    n_per_hh = st.groupby("HH")["T"].nunique()
    for region in REGIONS:
        rr = reg[reg["REGIONID"] == region]
        if rr.empty:
            continue
        for d, g in rr.groupby(rr["PERIOD"].dt.date):
            gp = g[(g["PERIOD"].dt.strftime("%H:%M") > PEAK_START) & (g["PERIOD"].dt.strftime("%H:%M") <= PEAK_END)]
            if gp.empty:
                continue
            row = gp.loc[gp["TOTALDEMAND"].idxmax()]
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
                "region": region, "date": d.strftime("%a %d %b"), "time": hh,
                "demand": round(demand), "pd_rrp": round(float(row["RRP"]), 2),
                "avail": round(avail), "vre": round(vre), "import_cap": None if math.isnan(impcap) else round(impcap),
                "cushion_pct": None if math.isnan(impcap) else round((avail + impcap - demand) / demand * 100, 1),
                "cheap_headroom": None if math.isnan(impcap) else round(avail300 + impcap - demand),
                "pivotal": pivotal[:4],
            })
    return out


def forward_reads(fwd: list[dict]) -> list[dict]:
    out = []
    for f in fwd:
        name = f["region"].rstrip("1")
        if f["cheap_headroom"] is not None and f["cheap_headroom"] < 300:
            who = ", ".join(p["portfolio"] for p in f["pivotal"][:2])
            out.append({"region": f["region"], "level": 3 if f["cheap_headroom"] < 0 else 2, "forward": True, "text":
                f"{name} {f['date']} {f['time']}: forecast demand {fmt_mw(f['demand'])} vs yesterday's offers leaves "
                f"{fmt_mw(f['cheap_headroom'])} of sub-$300 headroom (predispatch {f['pd_rrp']:,.0f}$/MWh)"
                + (f"; pivotal: {who}." if who else ".")})
        elif f["pivotal"]:
            who = ", ".join(p["portfolio"] for p in f["pivotal"][:2])
            out.append({"region": f["region"], "level": 2, "forward": True, "text":
                f"{name} {f['date']} {f['time']}: {who} would be pivotal at forecast demand {fmt_mw(f['demand'])} "
                f"if offers match yesterday's (predispatch ${f['pd_rrp']:,.0f})."})
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
    ranked = sorted(result["reads"], key=lambda x: -x["level"])[:5]
    if ranked:
        lines.append("")
        lines += [f"- {x['text']}" for x in ranked]
    return "\n".join(lines)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    no_push = "--no-push" in sys.argv
    allow_partial = "--allow-partial" in sys.argv
    now = datetime.now(nw.NEM_TZ).replace(tzinfo=None)
    day = args[0] if args else (now - timedelta(days=1)).strftime("%Y%m%d")
    day_iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"

    state = nw.read_state(STATE_FILE, default={}) or {}
    if state.get("last_trading_day") == day_iso and not force:
        log(f"already processed {day_iso} - skipping")
        return

    try:
        result, hist, stack = analyse(day)
    except FileNotFoundError as exc:
        log(f"{exc} - will retry on the next trigger")
        return

    if not result["availability_adjusted"] and not allow_partial and now.hour < 9:
        log("Next_Day_Dispatch missing - waiting for a later run rather than publishing untrimmed volumes")
        return

    fwd = forward_look(stack, datetime.strptime(day, "%Y%m%d"))
    result["forward"] = fwd
    result["reads"] = forward_reads(fwd) + result["reads"]
    result["generated"] = now.strftime("%Y-%m-%d %H:%M NEM")
    if not result["availability_adjusted"]:
        result["reads"].insert(0, {"region": "NEM", "level": 1, "text":
            "Next-day dispatch file wasn't available - wind/solar volumes are as bid, not trimmed to real availability."})

    out = write_dashboard(result)
    hist.to_csv(HISTORY_FILE, index=False)
    log(f"dashboard written: {out}")

    summary = ntfy_summary(result)
    print(summary)
    if not no_push:
        topics = nw.CONFIG.get("ntfy_topics", {})
        topic = topics.get("bidstack") or topics.get("market_read")
        url = nw.CONFIG.get("bidstack_dashboard_url")
        if topic:
            nw.push_ntfy(topic, summary + (f"\n\n{url}" if url else ""),
                         title=f"Bid stack - {day_iso}", tags=["bar_chart"])
    nw.write_state(STATE_FILE, {"last_trading_day": day_iso, "generated": result["generated"]})


if __name__ == "__main__":
    main()
