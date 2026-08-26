"""
Script: Cap Payout - Quarter to Date

The genuine running quarterly figure, replacing the crude "payout x 90
days" illustrative guess used elsewhere in this project. ASX $300 Cap
contracts settle over a full calendar quarter (Mar/Jun/Sep/Dec, per the
ASX product spec) using the same Cash Settlement Price formula as the
other cap scripts - just applied to every interval since the quarter
started, not one day.

Accumulates each settled day's prices (from Public_Prices) into a running
per-quarter state file, so this can be run once daily and show the true
quarter-to-date payout (ASX formula, all elapsed days in the quarter so
far), expressed as a share of the full quarter.

No standalone "yesterday" figure shown - you said it isn't needed (that's
what cap_yesterday.py used to cover; it's been removed).

State resets automatically at the start of each new calendar quarter.

Note on "different method than the original": the old system never actually
accumulated a real running quarter total - calc_cap_payout.py /
Create_Cap_payout_yesterday.py only ever computed ONE day's figure and
divided it by 90 as a rough guess at that day's share of the quarter. There
was no old script that kept a real day-by-day running sum to compare
against, so this one is new, not a re-derivation of something the old
system did differently. The /24 divisor you confirmed is correct is
applied here too (payout is additive across intervals, so /24-ing the
whole accumulated total is the same as /24-ing each day before adding it
up).

"qtr-to-date" is always divided by the FULL quarter day count (90/91/92),
never by days-elapsed-so-far - so on day 3 it's still ÷90, same as every
single day's own "adds approx to qrtr" figure elsewhere. That keeps it a
genuine running SUM of each day's own share (like-for-like, always the
same denominator), rather than a differently-scaled number that would
need its own separate conversion to compare against the daily figures.

(A "run-rate" full-quarter projection - current pace x full quarter length
- used to be shown here too. Dropped: it was my own addition, not in your
old system and not something you'd asked for by name.)

No $/MWh cap price shown anywhere in this script (yday used to show one) -
your old github scripts never computed one, and you confirmed you don't
want it added back in. Just the $ figures, which are the genuine additive
numbers.

Only pushes to ntfy if the qtr-to-date figure actually changed for any
region since the last run (tracked in cap_quarter_notify_state.json) - per
your request, not a repeat notification if no new settled day has actually
been ingested yet. Still prints to console every run regardless.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

import nemweb_common as nw

PUBLIC_PRICES_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Public_Prices/"

NEM_TZ = timezone(timedelta(hours=10))
STATE_FILE = "cap_quarter_state.json"


def fetch_day_prices(day: datetime, regions: list[str]) -> dict[str, list[float]] | None:
    compact = day.strftime("%Y%m%d")
    try:
        files = nw.list_nemweb_files(PUBLIC_PRICES_URL, rf"^PUBLIC_PRICES_{compact}0000_\d+\.zip$")
    except Exception as exc:
        print(f"[cap_quarter_to_date] WARNING: could not list {compact}: {exc}")
        return None
    if not files:
        return None
    try:
        df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(files[-1])), "DREGION")
    except Exception as exc:
        print(f"[cap_quarter_to_date] WARNING: could not parse {compact}: {exc}")
        return None
    df["RRP"] = pd.to_numeric(df["RRP"], errors="coerce")
    # AEMO's Public_Prices DREGION table publishes every interval TWICE (confirmed: 288 distinct
    # SETTLEMENTDATEs per region, 576 raw rows, identical RRP both times) - dedupe or every
    # payout $ figure accumulated here is inflated 2x. Cap price is unaffected (it's a ratio).
    df = df.drop_duplicates(subset=["SETTLEMENTDATE", "REGIONID"])
    return {r: df[df["REGIONID"] == r]["RRP"].dropna().tolist() for r in regions}


def main() -> None:
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    strike = cfg.get("cap_strike", 300)
    topic = cfg.get("ntfy_topics", {}).get("cap_payouts", "cap-payouts")

    now = datetime.now(NEM_TZ).replace(tzinfo=None)
    q_start, q_end, q_label = nw.quarter_bounds(now)
    yesterday = now - timedelta(days=1)

    state = nw.read_state(STATE_FILE, default={})
    if state.get("quarter_label") != q_label:
        print(f"[cap_quarter_to_date] New quarter ({q_label}) - resetting running totals.")
        state = {
            "quarter_label": q_label,
            "quarter_start": q_start.strftime("%Y-%m-%d"),
            "last_day_included": (q_start - timedelta(days=1)).strftime("%Y-%m-%d"),
            "prices": {r: [] for r in regions},
        }

    last_included = datetime.strptime(state["last_day_included"], "%Y-%m-%d")
    day = last_included + timedelta(days=1)
    days_added = 0

    while day <= yesterday:
        day_prices = fetch_day_prices(day, regions)
        if day_prices is None:
            break  # not published yet (or a gap) - stop, will retry from here next run
        for r in regions:
            state["prices"].setdefault(r, []).extend(day_prices.get(r, []))
        state["last_day_included"] = day.strftime("%Y-%m-%d")
        days_added += 1
        day += timedelta(days=1)

    nw.write_state(STATE_FILE, state)

    if days_added == 0 and not any(state["prices"].values()):
        print(f"[cap_quarter_to_date] No settled days available yet for {q_label}.")
        return

    days_elapsed = (datetime.strptime(state["last_day_included"], "%Y-%m-%d") - q_start).days + 1
    total_quarter_days = (q_end - q_start).days

    lines = [f"CAP PAYOUT QUARTER-TO-DATE - {q_label} (day {days_elapsed}/{total_quarter_days})"]
    current_values: dict[str, float] = {}
    for region in regions:
        _, qtd_payout_full = nw.cap_settlement(state["prices"].get(region, []), strike, interval_hours=5 / 60)
        qtd_payout_raw = qtd_payout_full / 24  # true $ accumulated over the elapsed days so far
        # "qtr-to-date" is always expressed as a share of the FULL quarter (÷ total_quarter_days),
        # even on day 3 of 90 - same treatment every single-day figure already gets ("adds approx
        # to qrtr"), so the running total is genuinely comparable to (and a running sum of) those
        # per-day shares, not a differently-scaled number. Otherwise it isn't like for like.
        qtd_payout = qtd_payout_raw / total_quarter_days

        lines.append(f"- {region}: qtr-to-date ${qtd_payout:,.2f}")
        current_values[region] = round(qtd_payout, 2)

    message = "\n".join(lines)
    print(message)

    # Only notify if the qtr-to-date figure actually changed since the last run - e.g. no
    # point re-notifying with the same number if a new settled day hasn't published yet.
    notify_state = nw.read_state("cap_quarter_notify_state.json", default={})
    if notify_state.get("values") == current_values:
        print("[cap_quarter_to_date] No change since last run - not pushing.")
        return

    nw.write_state("cap_quarter_notify_state.json", {"values": current_values})
    nw.push_ntfy(
        topic=topic,
        title=f"Cap payout - {q_label} quarter-to-date",
        message=message,
        tags=["bar_chart", "calendar"],
    )


if __name__ == "__main__":
    main()
