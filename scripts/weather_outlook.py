"""
Script: Weather & Generation Outlook

Rebuilt (2026-09-09) after market_read.py's schedule was removed - this pulls out just
the weather + wind/solar forecast half of what market_read.py used to compute, as its
own standalone daily script, plus a genuinely new piece: AEMO's own wind/solar
generation forecast (not just general weather).

Two data sources per region:
  1. Local weather forecast (temp, max wind) via open-meteo.com, one capital city per
     region as a demand-centre proxy. BOM's own feed is blocked from this connection
     (403) and open-meteo's BOM-specific model endpoint returned nulls for these
     coordinates, so this uses open-meteo's general forecast endpoint instead - same
     approach market_read.py used.
  2. AEMO's own wind/solar generation forecast - SS_WIND_UIGF / SS_SOLAR_UIGF
     ("Unconstrained Intermittent Generation Forecast", AEMO's own terminology) inside
     Short_Term_PASA_Reports' REGIONSOLUTION table (the same feed reserve_outlook.py
     already reads) - confirmed live to carry real, populated forecast values out to
     ~6 days ahead. This is the piece that wasn't in market_read.py at all before -
     general weather alone doesn't say what AEMO itself expects wind/solar output to
     actually be.

Shows tomorrow specifically (not every day of the ~6-day STPASA horizon) - one day's
worth of forward context each morning, not a multi-day dump.

Always pushes (like gas_spread_tracker.py/reserve_outlook.py) - this is forward
context you want every morning, not a threshold-gated alert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import nemweb_common as nw

STPASA_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Short_Term_PASA_Reports/"
STPASA_PATTERN = r"^PUBLIC_STPASA_\d{12}_\d+\.zip$"

NEM_TZ = timezone(timedelta(hours=10))

REGION_COORDS = {
    "NSW1": (-33.87, 151.21), "VIC1": (-37.81, 144.96), "QLD1": (-27.47, 153.03),
    "SA1": (-34.93, 138.60), "TAS1": (-42.88, 147.33),
}


def parse_interval_datetime(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


def fetch_weather(cfg: dict) -> dict:
    weather = {}
    try:
        lats = ",".join(str(c[0]) for c in REGION_COORDS.values())
        lons = ",".join(str(c[1]) for c in REGION_COORDS.values())
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lats, "longitude": lons, "daily": "temperature_2m_max,temperature_2m_min,wind_speed_10m_max",
                    "timezone": "Australia/Sydney", "forecast_days": 3},
            timeout=cfg.get("request_timeout_seconds", 30),
        )
        resp.raise_for_status()
        weather_data = resp.json()
        for region, day_data in zip(REGION_COORDS, weather_data):
            daily = day_data.get("daily", {})
            weather[region] = {
                "dates": daily.get("time", []),
                "max_temp": daily.get("temperature_2m_max", []),
                "min_temp": daily.get("temperature_2m_min", []),
                "max_wind_kmh": daily.get("wind_speed_10m_max", []),
            }
    except Exception as exc:
        print(f"[weather_outlook] WARNING: could not fetch weather forecast: {exc}")
    return weather


def registry_capacity_by_region(registry: nw.Registry, fuel: str) -> dict[str, float]:
    """Static total registered capacity per region for one fuel type, from the registry -
    NOT AEMO's own SS_WIND_CAPACITY/SS_SOLAR_CAPACITY fields, which turned out (confirmed
    live) to be some fluctuating semi-scheduled-offer subset, not the whole fleet: NSW1
    showed SS_WIND_CAPACITY swinging 258-966MW interval to interval while SS_WIND_UIGF hit
    1437MW - a UIGF that's supposedly "unconstrained" (uncapped by commercial constraints)
    exceeding its own capacity denominator is the tell that field isn't a stable total.
    The registry's own summed CAPACITY (2598MW for NSW1 wind) is what UIGF is actually a
    sane fraction of.
    """
    if registry.fuel_info is None or "CAPACITY" not in registry.fuel_info.columns:
        return {}
    fcol = next((c for c in registry.fuel_info.columns if c.lower() == "fuel"), None)
    if fcol is None:
        return {}
    rows = registry.fuel_info[registry.fuel_info[fcol].str.lower() == fuel.lower()]
    return rows.groupby("REGIONID")["CAPACITY"].sum().to_dict()


def main() -> None:
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    topic = cfg.get("ntfy_topics", {}).get("weather", "weather-outlook")

    now = datetime.now(NEM_TZ).replace(tzinfo=None)
    tomorrow = (now + timedelta(days=1)).date()

    try:
        files = nw.get_latest_files(STPASA_URL, STPASA_PATTERN, n=1)
    except Exception as exc:
        print(f"[weather_outlook] ERROR listing NEMWEB directory: {exc}")
        return

    stpasa_tables = nw.parse_mms_zip(nw.download_bytes(files[-1]))
    df = nw.get_table(stpasa_tables, "REGIONSOLUTION")
    df["_interval_dt"] = df["INTERVAL_DATETIME"].apply(parse_interval_datetime)
    for col in ("SS_WIND_UIGF", "SS_SOLAR_UIGF"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    registry = nw.load_registry()
    wind_capacity = registry_capacity_by_region(registry, "Wind")
    solar_capacity = registry_capacity_by_region(registry, "Solar")

    tomorrow_df = df[df["_interval_dt"].dt.date == tomorrow]
    weather = fetch_weather(cfg)

    lines = [f"Weather & generation outlook for tomorrow ({tomorrow.strftime('%a %d-%b')}):"]
    for region in regions:
        parts = []
        fc = weather.get(region)
        if fc and fc.get("dates"):
            idx = 1 if len(fc["dates"]) > 1 else 0  # index 1 = tomorrow (0 = today)
            max_t = fc["max_temp"][idx] if idx < len(fc.get("max_temp", [])) else None
            min_t = fc["min_temp"][idx] if idx < len(fc.get("min_temp", [])) else None
            wind = fc["max_wind_kmh"][idx] if idx < len(fc.get("max_wind_kmh", [])) else None
            if min_t is not None and max_t is not None:
                parts.append(f"{min_t:.0f}-{max_t:.0f}C")
            if wind is not None:
                parts.append(f"{wind:.0f}km/h max wind")

        region_rows = tomorrow_df[tomorrow_df["REGIONID"] == region]
        if not region_rows.empty:
            wind_cap = wind_capacity.get(region)
            solar_cap = solar_capacity.get(region)
            if wind_cap and wind_cap > 0:
                wind_avg_pct = region_rows["SS_WIND_UIGF"].mean() / wind_cap * 100
                wind_peak_pct = region_rows["SS_WIND_UIGF"].max() / wind_cap * 100
                parts.append(f"wind avg {wind_avg_pct:.0f}% / peak {wind_peak_pct:.0f}% of {wind_cap:,.0f}MW capacity")
            if solar_cap and solar_cap > 0:
                solar_peak_pct = region_rows["SS_SOLAR_UIGF"].max() / solar_cap * 100
                parts.append(f"solar peak {solar_peak_pct:.0f}% of {solar_cap:,.0f}MW capacity")

        if parts:
            lines.append(f"  {region}: " + ", ".join(parts))
        else:
            lines.append(f"  {region}: no data")

    message = "\n".join(lines)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title="Weather & generation outlook",
        message=message,
        tags=["partly_sunny", "wind_blowing_face"],
    )


if __name__ == "__main__":
    main()
