"""
Script: Spot Spike Alerter

Watches DISPATCHPRICE (same DispatchIS zip as cap_intraday.py) and alerts
when any region's RRP crosses one of a set of ascending thresholds.

Debounced: while a region's price stays within the same tier (e.g. sitting
at $2,000/MWh, above the $1,000 threshold but below the $5,000 one), it
does not re-alert every 5 minutes. It only alerts again when price crosses
into a *higher* tier, or after dropping back below the lowest threshold and
spiking again (a fresh episode).

Note: the NEM Market Price Cap itself rises each financial year (indexed,
$14,700 in 2019-20 -> $17,500 in 2024-25) - the top threshold below is a
config value, not the live MPC, so bump `spot_spike_thresholds` in
config.json each July rather than assuming $15,000 always means "at the cap".
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

import nemweb_common as nw

DISPATCHIS_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
DISPATCHIS_PATTERN = r"^PUBLIC_DISPATCHIS_\d{12}_\d{16}\.zip$"

NEM_TZ = timezone(timedelta(hours=10))
STATE_FILE = "spot_spike_state.json"
LOOKBACK_FILES = 30

DEFAULT_THRESHOLDS = [300, 1000, 5000, 15000]


def parse_settlementdate(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


def tier_for_price(price: float, thresholds: list[float]) -> int:
    """Highest threshold index the price is at or above; -1 if below all of them."""
    tier = -1
    for i, t in enumerate(thresholds):
        if price >= t:
            tier = i
    return tier


def main() -> None:
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    thresholds = sorted(cfg.get("spot_spike_thresholds", DEFAULT_THRESHOLDS))
    topic = cfg.get("ntfy_topics", {}).get("spot", "spot-alerts")

    state = nw.read_state(STATE_FILE, default={})
    active_tier = state.get("active_tier", {r: -1 for r in regions})
    for r in regions:
        active_tier.setdefault(r, -1)
    last_settlementdate_str = state.get("last_settlementdate")
    last_settlementdate = parse_settlementdate(last_settlementdate_str) if last_settlementdate_str else None

    try:
        files = nw.get_latest_files(DISPATCHIS_URL, DISPATCHIS_PATTERN, n=LOOKBACK_FILES)
    except Exception as exc:
        print(f"[spot_spike] ERROR listing NEMWEB directory: {exc}")
        sys.exit(1)

    alerts: list[str] = []
    newest_seen = last_settlementdate

    for url in files:
        try:
            table = nw.download_and_get_table(url, "DISPATCHPRICE")
        except Exception as exc:
            print(f"[spot_spike] WARNING: skipping {url}: {exc}")
            continue
        if table.empty or "SETTLEMENTDATE" not in table.columns:
            continue

        table["_settlementdate"] = table["SETTLEMENTDATE"].apply(parse_settlementdate)
        table["RRP"] = pd.to_numeric(table["RRP"], errors="coerce")

        interval_time = table["_settlementdate"].iloc[0]
        if last_settlementdate is not None and interval_time <= last_settlementdate:
            continue  # already processed this interval on a previous run

        for _, row in table.sort_values("REGIONID").iterrows():
            region = str(row.get("REGIONID", "")).strip()
            if region not in active_tier:
                continue
            try:
                rrp = float(row["RRP"])
            except (TypeError, ValueError):
                continue

            new_tier = tier_for_price(rrp, thresholds)
            old_tier = active_tier[region]
            if new_tier > old_tier:
                alerts.append(f"{region}: price spike ${rrp:,.0f}")
                active_tier[region] = new_tier
            elif new_tier < old_tier:
                # Dropped a tier - also alert on the way down now, and always update the
                # tracked tier to match (not just when it falls all the way below every
                # threshold) - otherwise a drop from tier 2 to tier 1 (still elevated) never
                # got recorded, so climbing back to tier 2 later wouldn't re-alert.
                alerts.append(f"{region}: price drop ${rrp:,.0f}")
                active_tier[region] = new_tier

        if newest_seen is None or interval_time > newest_seen:
            newest_seen = interval_time

    state["active_tier"] = active_tier
    if newest_seen is not None:
        state["last_settlementdate"] = newest_seen.strftime("%Y/%m/%d %H:%M:%S")
    nw.write_state(STATE_FILE, state)

    if not alerts:
        print("[spot_spike] No new threshold crossings.")
        return

    message = "Spot price spike:\n" + "\n".join(f"  {a}" for a in alerts)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title=f"Spot price spike ({len(alerts)} region-event(s))",
        message=message,
        priority="high",
        tags=["rotating_light"],
    )


if __name__ == "__main__":
    main()
