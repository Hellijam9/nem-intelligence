"""
Script: Gas Benchmark Spread Tracker (new, from the QED historical remap)

Tracks domestic east-coast gas hub prices against the international LNG
benchmark - per the remap, this spread is the single most fragile
assumption sitting in the QED build reference's own forward-view notes
("International LNG materially higher than domestic gas due to Middle
East conflict... watch into winter"), and the 8-year QED history shows
this spread leads domestic contract repricing by weeks to months in every
major gas-driven price event (2018, the 2021 Callide-quarter gas spike,
the full 2022 crisis).

Domestic side - two real, free, NEMWEB-published sources, confirmed live:
    STTM (Sydney/Brisbane/Adelaide hub prices) - STTM/CurrentDay.zip,
        report int654_v1_provisional_market_price_rpt (plain CSV, not the
        usual MMS I/D wrapper - AEMO's gas-market reports use a different
        export convention than the DISPATCH/electricity side entirely).
    GSH Wallumbilla benchmark price - GSH/Benchmark_Price/
        PUBLIC_WALLUMBILLABENCHMARKPRICE_YYYYMMDD.zip, PRODUCT_LOCATION
        "WAL" (the currently-active code; "WALLUMBILLA" appears in the
        same file but stopped updating years ago - a naming migration
        worth knowing if this ever looks stale).

Known gap: DWGM (Victorian) hub price was not found in a clearly
identifiable free NEMWEB report in the time available to build this - the
domestic average below is STTM (3 hubs) + Wallumbilla only, not the full
5-hub set QED reports quote. Worth revisiting if VIC-specific gas pricing
becomes important.

International side: there is no free feed of the *official* JKM benchmark
(Platts' actual physically-assessed price) - that's a paid Platts/Argus/ICE
product. What IS fetched live now is tradingeconomics.com's JKM figure,
scraped from an embedded JSON blob on their public commodity page
(`TEChartsMeta`, matched by name "LNG JKM"). Per Trading Economics' own
disclosure, this tracks an OTC/CFD instrument referencing the JKM market,
not the official benchmark tick itself - "a general market reference
only... not independently verified." Good enough for this alert's
purpose (you decided so, after being shown the caveat). Falls back to
config.json's manually-set `international_lng_jkm_usd_mmbtu` if the live
scrape ever fails (site change, network issue, etc) - `last_updated` and
the 14-day staleness check now only matter for that fallback case.

AUD/USD is fetched live too, from frankfurter.app (ECB-based, no API key,
genuinely official/free unlike JKM) - falls back to config.json's
`audusd_fx_rate` the same way if that call fails.

This script converts JKM (USD/MMBtu) to an AUD/GJ equivalent
(1 MMBtu = 1.055 GJ) for direct comparison against the domestic hubs.
"""

from __future__ import annotations

import zipfile
import io
import re
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import nemweb_common as nw

STTM_CURRENT_DAY_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/STTM/CurrentDay.zip"
GSH_BENCHMARK_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/GSH/Benchmark_Price/"
FX_RATE_URL = "https://api.frankfurter.app/latest?from=AUD&to=USD"
JKM_PAGE_URL = "https://tradingeconomics.com/commodity/liquefied-natural-gas-japan-korea"
BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

NEM_TZ = timezone(timedelta(hours=10))
MMBTU_TO_GJ = 1.055
STALE_DAYS = 14


def get_sttm_prices() -> pd.DataFrame:
    """Latest provisional market price per hub from STTM/CurrentDay.zip."""
    b = nw.download_bytes(STTM_CURRENT_DAY_URL)
    with zipfile.ZipFile(io.BytesIO(b)) as zf:
        candidates = sorted(n for n in zf.namelist() if "int654" in n and "provisional_market_price" in n)
        if not candidates:
            return pd.DataFrame()
        with zf.open(candidates[-1]) as f:  # sorted filename = latest report_datetime suffix
            df = pd.read_csv(f)
    df["gas_date"] = pd.to_datetime(df["gas_date"], format="%d %b %Y")
    df["provisional_price"] = pd.to_numeric(df["provisional_price"], errors="coerce")
    return df


def get_live_jkm_price() -> float | None:
    """Live JKM (USD/MMBtu) scraped from tradingeconomics.com's embedded TEChartsMeta JSON
    blob, matched by name "LNG JKM" - a CFD-tracked proxy, not the official Platts benchmark
    tick, per their own disclosure. Good enough for this alert's purpose."""
    try:
        resp = requests.get(
            JKM_PAGE_URL,
            timeout=nw.CONFIG.get("request_timeout_seconds", 30),
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        resp.raise_for_status()
        match = re.search(r"TEChartsMeta\s*=\s*(\[.*?\]);", resp.text)
        if not match:
            print("[gas_spread_tracker] WARNING: TEChartsMeta not found on JKM page - site may have changed.")
            return None
        for entry in json.loads(match.group(1)):
            if entry.get("name") == "LNG JKM":
                return float(entry["value"])
        print("[gas_spread_tracker] WARNING: 'LNG JKM' entry not found in TEChartsMeta.")
        return None
    except Exception as exc:
        print(f"[gas_spread_tracker] WARNING: live JKM fetch failed ({exc}) - falling back to config.json value.")
        return None


def get_live_fx_rate() -> float | None:
    """Live AUD/USD rate (1 AUD = X USD, same convention as the config.json fallback value).
    Free, public, no API key - unlike JKM there's no reason this needs manual upkeep."""
    try:
        resp = requests.get(FX_RATE_URL, timeout=nw.CONFIG.get("request_timeout_seconds", 30), allow_redirects=True)
        resp.raise_for_status()
        return float(resp.json()["rates"]["USD"])
    except Exception as exc:
        print(f"[gas_spread_tracker] WARNING: live FX fetch failed ({exc}) - falling back to config.json value.")
        return None


def get_wallumbilla_price() -> float | None:
    """Latest 'WAL' (Wallumbilla) benchmark price for the most recent gas date."""
    today_compact = datetime.now(NEM_TZ).strftime("%Y%m%d")
    files = nw.list_nemweb_files(GSH_BENCHMARK_URL, rf"^PUBLIC_WALLUMBILLABENCHMARKPRICE_{today_compact}\.zip$")
    if not files:
        # today's file may not have published yet - fall back to the most recent available
        files = nw.list_nemweb_files(GSH_BENCHMARK_URL, r"^PUBLIC_WALLUMBILLABENCHMARKPRICE_\d{8}\.zip$")
    if not files:
        return None
    df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(files[-1])), "BENCHMARK_PRICE")
    df["GAS_DATE"] = pd.to_datetime(df["GAS_DATE"])
    df["BENCHMARK_PRICE"] = pd.to_numeric(df["BENCHMARK_PRICE"], errors="coerce")
    wal = df[df["PRODUCT_LOCATION"] == "WAL"].sort_values("GAS_DATE")
    if wal.empty:
        return None
    return float(wal.iloc[-1]["BENCHMARK_PRICE"])


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("gas_spread", "gas-spread-alerts")
    gas_cfg = cfg.get("gas_benchmark", {})

    jkm_usd_mmbtu = get_live_jkm_price()
    jkm_is_live = jkm_usd_mmbtu is not None
    if jkm_usd_mmbtu is None:
        jkm_usd_mmbtu = gas_cfg.get("international_lng_jkm_usd_mmbtu")  # live scrape failed - fall back

    fx_rate = get_live_fx_rate()
    fx_is_live = fx_rate is not None
    if fx_rate is None:
        fx_rate = gas_cfg.get("audusd_fx_rate")  # live fetch failed - fall back to the manual config value

    last_updated_str = gas_cfg.get("last_updated")

    sttm = get_sttm_prices()
    domestic_prices = {}
    if not sttm.empty:
        latest_date = sttm["gas_date"].max()
        latest = sttm[sttm["gas_date"] == latest_date]
        for _, row in latest.iterrows():
            domestic_prices[row["hub_name"]] = row["provisional_price"]

    wal_price = get_wallumbilla_price()
    if wal_price is not None:
        domestic_prices["Wallumbilla"] = wal_price

    if not domestic_prices:
        print("[gas_spread_tracker] Could not retrieve any domestic hub prices - aborting.")
        return

    domestic_avg = sum(domestic_prices.values()) / len(domestic_prices)
    nw.write_state("gas_spread_state.json", {
        "checked_at": datetime.now().isoformat(),
        "domestic_prices": domestic_prices,
        "domestic_avg": domestic_avg,
        "jkm_usd_mmbtu": jkm_usd_mmbtu,
        "audusd_fx_rate": fx_rate,
    })

    lines = ["Domestic east-coast gas hub prices ($/GJ):"]
    for hub, price in domestic_prices.items():
        lines.append(f"  {hub}: ${price:.2f}/GJ")
    lines.append(f"  Average: ${domestic_avg:.2f}/GJ")

    if jkm_usd_mmbtu is None or fx_rate is None:
        lines.append("\n(No international LNG benchmark configured - set gas_benchmark.international_lng_jkm_usd_mmbtu "
                      "and .audusd_fx_rate in config.json to enable the spread comparison.)")
        message = "\n".join(lines)
        print(message)
        nw.push_ntfy(topic=topic, title="Domestic gas hub prices", message=message, tags=["fuelpump"])
        return

    jkm_aud_gj = (jkm_usd_mmbtu / fx_rate) / MMBTU_TO_GJ
    spread = jkm_aud_gj - domestic_avg
    nw.write_state("gas_spread_state.json", {
        "checked_at": datetime.now().isoformat(),
        "domestic_prices": domestic_prices,
        "domestic_avg": domestic_avg,
        "jkm_usd_mmbtu": jkm_usd_mmbtu,
        "audusd_fx_rate": fx_rate,
        "jkm_aud_gj": jkm_aud_gj,
        "spread_aud_gj": spread,
        # jkm_is_live is what actually determines staleness now - benchmark_last_updated is
        # the static config.json date, which is meaningless (and looks permanently stale)
        # whenever the live scrape is working, so downstream consumers (market_read.py) must
        # check jkm_is_live first, not just compare this date blindly.
        "jkm_is_live": jkm_is_live,
        "fx_is_live": fx_is_live,
        "benchmark_last_updated": last_updated_str,
    })

    # Staleness only means something when running on the manual config fallback - a live
    # scrape can't be stale, it's fresh as of right now.
    stale_note = ""
    if not jkm_is_live and last_updated_str:
        last_updated = datetime.strptime(last_updated_str, "%Y-%m-%d")
        age_days = (datetime.now() - last_updated).days
        if age_days > STALE_DAYS:
            stale_note = f" [STALE FALLBACK - config.json JKM last updated {age_days}d ago]"

    jkm_note = "live, tradingeconomics.com" if jkm_is_live else "config fallback - live scrape failed"
    fx_note = "live" if fx_is_live else "config fallback - live fetch failed"
    lines.append(f"\nInternational LNG (JKM): ${jkm_usd_mmbtu:.2f}/MMBtu ({jkm_note}) = ${jkm_aud_gj:.2f}/GJ equivalent"
                 f" (@ AUD/USD {fx_rate:.4f}, {fx_note}){stale_note}")
    lines.append(f"Spread (international - domestic): ${spread:+.2f}/GJ")

    message = "\n".join(lines)
    print(message)

    # Reports every day unconditionally now, no threshold gate - this is a daily reading, not
    # an alert. Whether a given day's spread is actually notable against history is judged
    # separately by causal_rules.py's rule_gas_spread (which reads this state file), not here.
    nw.push_ntfy(
        topic=topic,
        title=f"Gas spread {spread:+.1f}/GJ" + (" [STALE]" if stale_note else ""),
        message=message,
        tags=["fuelpump"],
    )


if __name__ == "__main__":
    main()
