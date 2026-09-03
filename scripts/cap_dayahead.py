"""
Script: Cap Payout - Day Ahead (projected)

Reworked from the old GitHub Actions version, which pulled a combined
actual+predispatch feed from Neopoint in one shot. That source isn't
available for this build, so this reconstructs the same "today, midnight
to midnight" figure from two real NEMWEB sources instead:
  - DispatchIS_Reports (actual, 5-min) for midnight -> now
  - Predispatch_Reports (forecast, 30-min) for now -> midnight tonight
...then adds them for one "expected cap payout today" figure per region,
plus tomorrow's full-day forecast as a bonus (Predispatch covers ~31h
ahead, so a full day-ahead figure is available, not just "the rest of today").

Deliberately self-contained/stateless (unlike cap_intraday.py, this does
NOT read cap_intraday.py's state file) - it re-fetches and recomputes the
actual-so-far component from scratch every run, same as the old script
did. This is what lets it run correctly on GitHub Actions (a fresh VM
every run, nothing persisted between runs) as well as locally.

Note on the old script: it divided the summed payout by 24 at the end,
despite a comment claiming "/60". You confirmed the /24 figure is the
correct one to use, so every $ figure here is divided by 24, full stop -
no alternate/bracketed figure alongside it.

No $/MWh cap price shown anywhere (there used to be one) - your old
github scripts never computed one, and you confirmed you don't want it
added back in. Just the $ payout figures.

Also shows both forward-looking figures (today, tomorrow) in terms of the
quarter as a whole: reads cap_quarter_to_date.py's settled quarter-to-date
state (up to yesterday) as a baseline, then projects what the running
quarterly total would become if today's/tomorrow's numbers eventuate as
forecast. This is explicitly a projection, not a settlement - only the
baseline (up to yesterday) is real settled data; today/tomorrow are still
forecast until they actually happen. If cap_quarter_to_date.py hasn't been
run yet this quarter, the baseline is treated as empty/zero with a note.

Only pushes to ntfy if today's/tomorrow's figure actually changed for any
region since the last run (tracked in cap_dayahead_notify_state.json) - per
your request, no point getting the same $0.00 notification every 30 minutes
on a calm day. Still prints to console every run regardless, for debugging.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pandas as pd

import nemweb_common as nw

DISPATCHIS_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/DispatchIS_Reports/"
DISPATCHIS_PATTERN = r"^PUBLIC_DISPATCHIS_(\d{12})_\d{16}\.zip$"

PREDISPATCH_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Predispatch_Reports/"
PREDISPATCH_PATTERN = r"^PUBLIC_PREDISPATCH_\d{12}_\d{14}_LEGACY\.zip$"

NEM_TZ = timezone(timedelta(hours=10))
PREDISPATCH_PERIOD_HOURS = 0.5  # Predispatch periods are 30-min, unlike DispatchIS's 5-min


def parse_price_datetime(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")


def actual_prices_since_midnight(regions: list[str], now: datetime) -> dict[str, list[float]]:
    """Fresh (not state-dependent) list of today's actual 5-min DispatchIS prices, midnight to now."""
    midnight_today = datetime(now.year, now.month, now.day)
    prices: dict[str, list[float]] = {r: [] for r in regions}

    files = nw.list_nemweb_files(DISPATCHIS_URL, DISPATCHIS_PATTERN)
    todays_files = []
    for url in files:
        m = re.search(r"PUBLIC_DISPATCHIS_(\d{12})_", url)
        if not m:
            continue
        file_dt = datetime.strptime(m.group(1), "%Y%m%d%H%M")
        if midnight_today <= file_dt <= now:
            todays_files.append(url)

    for url in todays_files:
        try:
            table = nw.download_and_get_table(url, "DISPATCHPRICE")
        except Exception as exc:
            print(f"[cap_dayahead] WARNING: skipping {url}: {exc}")
            continue
        table["RRP"] = pd.to_numeric(table["RRP"], errors="coerce")
        for _, row in table.iterrows():
            region = str(row.get("REGIONID", "")).strip()
            if region not in prices:
                continue
            try:
                prices[region].append(float(row["RRP"]))
            except (TypeError, ValueError):
                continue
    return prices


def windowed_prices(df: pd.DataFrame, start: datetime, end: datetime) -> pd.Series:
    return df[(df["_period_dt"] >= start) & (df["_period_dt"] < end)]["RRP"]


def main() -> None:
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    strike = cfg.get("cap_strike", 300)
    topic = cfg.get("ntfy_topics", {}).get("cap_payouts", "cap-payouts")

    now = datetime.now(NEM_TZ).replace(tzinfo=None)
    midnight_tonight = datetime(now.year, now.month, now.day) + timedelta(days=1)
    midnight_day_after = midnight_tonight + timedelta(days=1)

    q_start, q_end, q_label = nw.quarter_bounds(now)
    total_quarter_days = (q_end - q_start).days
    quarter_state = nw.read_state("cap_quarter_state.json", default=None)
    if quarter_state and quarter_state.get("quarter_label") == q_label:
        baseline_prices = quarter_state.get("prices", {})
        baseline_note = f"settled through {quarter_state.get('last_day_included', '?')}"
    else:
        baseline_prices = {}
        baseline_note = "no settled quarter-to-date baseline yet - run cap_quarter_to_date.py first"

    print("[cap_dayahead] Computing actual-so-far from today's DispatchIS files...")
    actual_prices = actual_prices_since_midnight(regions, now)

    try:
        pd_files = nw.get_latest_files(PREDISPATCH_URL, PREDISPATCH_PATTERN, n=1)
    except Exception as exc:
        print(f"[cap_dayahead] ERROR listing Predispatch directory: {exc}")
        return

    pd_df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(pd_files[-1])), "PDREGION")
    pd_df["RRP"] = pd.to_numeric(pd_df["RRP"], errors="coerce")
    pd_df["_period_dt"] = pd_df["PERIODID"].apply(parse_price_datetime)

    header = f"CAP PAYOUT TODAY & TOMORROW (projected) - vs {q_label} ({baseline_note})"
    region_lines: dict[str, list[str]] = {}
    current_values: dict[str, list[float]] = {}
    for region in regions:
        lines = region_lines.setdefault(region, [])
        region_baseline = baseline_prices.get(region, [])
        _, baseline_payout = nw.cap_settlement(region_baseline, strike, interval_hours=5 / 60)

        region_actual = actual_prices.get(region, [])
        _, actual_payout = nw.cap_settlement(region_actual, strike, interval_hours=5 / 60)

        region_pd = pd_df[pd_df["REGIONID"] == region]
        remainder_prices = windowed_prices(region_pd, now, midnight_tonight) if not region_pd.empty else pd.Series(dtype=float)
        _, remainder_payout = nw.cap_settlement(remainder_prices, strike, interval_hours=PREDISPATCH_PERIOD_HOURS)

        combined_payout_full = actual_payout + remainder_payout

        tomorrow_prices = windowed_prices(region_pd, midnight_tonight, midnight_day_after) if not region_pd.empty else pd.Series(dtype=float)
        _, tomorrow_payout_full = nw.cap_settlement(tomorrow_prices, strike, interval_hours=PREDISPATCH_PERIOD_HOURS)

        # Projected quarter-to-date $ if today's/tomorrow's forecast numbers eventuate as
        # calculated. No $/MWh price computed for these - see note below on why.
        through_today_payout_full = baseline_payout + combined_payout_full
        through_tomorrow_payout_full = through_today_payout_full + tomorrow_payout_full

        # /24 divisor - per your confirmation this is the correct figure. Applied to every
        # $ figure here (payout is additive across intervals, so /24-ing the baseline and
        # /24-ing today/tomorrow separately then adding them is equivalent to /24-ing the
        # combined total) - no alternate figure shown, just this one, divided by 24.
        combined_payout = combined_payout_full / 24
        tomorrow_payout = tomorrow_payout_full / 24

        # "adds to qrtr" gut-check, same as cap_intraday.py: this day's own
        # contribution divided by the *actual* day count of the current quarter (90/91/92
        # depending which quarter and leap years, via nemweb_common.quarter_bounds) - not a
        # flat 90. Wherever the old scripts used a literal 90, this is what it meant.
        today_qrtr_add = combined_payout / total_quarter_days
        tomorrow_qrtr_add = tomorrow_payout / total_quarter_days

        # "Qtr-to-date"/running totals are always expressed as a share of the FULL quarter
        # (÷ total_quarter_days), same as every single day's own "adds approx to qrtr" figure -
        # not ÷24 alone. That keeps this a genuine running SUM of each day's own share
        # (like-for-like, same denominator throughout), matching cap_quarter_to_date.py.
        baseline_payout_scaled = (baseline_payout / 24) / total_quarter_days
        through_today_payout = (through_today_payout_full / 24) / total_quarter_days
        through_tomorrow_payout = (through_tomorrow_payout_full / 24) / total_quarter_days

        lines.append(f"- {region}")
        # Baseline shown explicitly (not just folded into "qtr-to-date now") so the actual
        # settled quarter-to-date accumulation this is being added onto is visible, not hidden.
        lines.append(f"    Qtr-to-date before today: ${baseline_payout_scaled:,.2f}")
        lines.append(
            f"    Today:    ${combined_payout:,.2f}  "
            f"-  adds approx. ${today_qrtr_add:,.2f} to qrtr"
        )
        lines.append(
            f"    Tomorrow: ${tomorrow_payout:,.2f}  "
            f"-  adds approx. ${tomorrow_qrtr_add:,.2f} to qrtr"
        )

        current_values[region] = [round(combined_payout, 2), round(tomorrow_payout, 2)]

    full_message = header + "\n" + "\n".join(line for region in regions for line in region_lines[region])
    print(full_message)

    # Only notify if the outcome is genuinely different, not just time-dependent drift as
    # forecasts refresh every 30 min. Compared against the last NOTIFIED values (not just the
    # last run) so a slow drift still eventually crosses the threshold instead of resetting
    # every single run. A move only counts as significant if it's >=10% different, OR a payout
    # newly appeared/disappeared entirely (0 -> nonzero or nonzero -> 0, where % change is
    # undefined) - that's always worth a push.
    def is_significant(prev: float, curr: float, threshold: float = 0.10) -> bool:
        if prev == 0 and curr == 0:
            return False
        if prev == 0 or curr == 0:
            return True
        return abs(curr - prev) / abs(prev) >= threshold

    today_str = now.strftime("%Y-%m-%d")
    notify_state = nw.read_state("cap_dayahead_notify_state.json", default={})
    prev_date = notify_state.get("date")
    prev_values = notify_state.get("values", {})

    # "Today"/"tomorrow" are just labels that shift meaning every midnight - the day that was
    # "tomorrow" becomes "today". Comparing raw labels across that boundary would compare a
    # brand-new day's tiny figure against the completed previous day's final total, which
    # always looks like a huge change even though nothing unusual happened - just the calendar
    # rolling over. Instead: if exactly one day has passed since the last notification, shift
    # the baseline so the new "today" is compared against what it was already forecast to be
    # (yesterday's "tomorrow" figure for that same calendar day), and the new "tomorrow" (a day
    # never forecast before) starts fresh with no prior data. A bigger gap (missed runs) or no
    # prior date at all also starts fresh, since there's no reliable prior figure to compare to.
    if prev_date == today_str:
        prev_for_compare = prev_values
    else:
        days_passed = None
        if prev_date:
            try:
                days_passed = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(prev_date, "%Y-%m-%d")).days
            except ValueError:
                days_passed = None
        if days_passed == 1:
            prev_for_compare = {r: [v[1], 0.0] for r, v in prev_values.items()}
        else:
            prev_for_compare = {}

    changed_regions = []
    for region in regions:
        today_val, tomorrow_val = current_values[region]
        prev_today, prev_tomorrow = prev_for_compare.get(region, [0, 0])
        if is_significant(prev_today, today_val) or is_significant(prev_tomorrow, tomorrow_val):
            changed_regions.append(region)

    if not changed_regions:
        print("[cap_dayahead] No move >=10% (or new/vanished payout) since last notification - not pushing.")
        return

    # Trim the pushed message down to only the region(s) that actually moved >=10% - the
    # other regions' unchanged numbers aren't worth repeating in every notification.
    trimmed_message = header + "\n" + "\n".join(line for region in changed_regions for line in region_lines[region])

    nw.write_state("cap_dayahead_notify_state.json", {"date": today_str, "values": current_values})
    nw.push_ntfy(
        topic=topic,
        title="Cap payout - today (actual+forecast) & tomorrow",
        message=trimmed_message,
        tags=["crystal_ball", "bar_chart"],
    )


if __name__ == "__main__":
    main()
