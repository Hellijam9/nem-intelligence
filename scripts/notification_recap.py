"""
Script: Notification Recap (morning report)

Runs once a day at 7:30am Sydney time, weekdays only. Does not pull any live
NEMWEB data itself - reads back the shared notification log (every push_ntfy()
call across every script in this system appends to state/notification_log.jsonl)
and assembles two sections:

  OVERNIGHT - what happened, window = 4pm the previous day to now (4pm Friday
  to 7:30am Monday on Mondays, since there's no recap on Sat/Sun).
    - Anything without an explicit "cleared" signal in its own text is always
      shown as-is, however brief - a DUID dropping for 5 minutes and
      recovering is still real and still reported. This covers discrete
      physical events (customer_watcher, scada_drop_monitor, rebid_reconciler)
      and interconnector_monitor (which only ever pushes while at/near limit -
      it has no "back to normal" message to pair against, so net-effect
      filtering can't tell a brief flicker from a stale reading; showing
      everything is the only correct default).
    - Only sources that actually push an explicit "cleared" line
      (predispatch_tracker's "back under $300", spot_spike's "price drop")
      are net-effect filtered: if something crossed a threshold and cleared
      again within the window, it's dropped; only what's still active at
      cutoff is shown.
    - Also includes a predispatch_tracker "forecast vs actual" check: for every
      period predispatch_tracker forecast above $300 overnight, did the actual
      price ever get there? Bundled per region ("did/didn't eventuate"), not a
      raw crossing/clearing log - this is a retrospective check against what
      already happened, so it belongs here, not in DAY AHEAD.

  DAY AHEAD - what's coming. Everything here is a live check each morning, not log-replay -
  the underlying scripts (cap_dayahead, predispatch_tracker, reserve_outlook) all only push
  when something moves/changes enough, so replaying their logs could show a days-old,
  sometimes partial-region snapshot (confirmed live: cap_dayahead's log was showing a 6-day-old
  VIC1/SA1/TAS1-only push with NSW1/QLD1 silently missing).
    - Cap payouts: quarter-to-date per region (read from cap_quarter_state.json,
      already-settled data, no fetch needed) plus a fresh today/tomorrow fetch,
      flagging a region only if it's actually paying out.
    - Predispatch forecast: genuine forward-looking check, live from the current
      Predispatch run - any upcoming period(s) forecast above $300, bundled per
      region into ranges. No comparison against actuals (that's the overnight
      section's job) - day ahead, not day before.
    - Price outlook: today's remaining forecast price range per region, live
      from Predispatch - nothing else in this report states this plainly.
    - Reserve outlook: "adequate reserve <date span>" per region, or the
      actual LOR condition/day as an exception, from a fresh STPASA fetch.
    - Units out today: filtered from pasa_monitor's own rolling cache
      (pasa_recent_windows.json) rather than re-fetching/re-diffing the
      ~250k-row MTPASA snapshot itself, which would just duplicate
      pasa_monitor's own job - the cache is already current, updated every
      time pasa_monitor runs (~3-hourly).
    - weather_outlook is still log-replay (its own push already happens
      same-morning, so it's never meaningfully stale) and keeps its own
      separate standalone push too - this is a second copy in the bundled
      report, not a replacement.

Deliberately excludes market_read (deleted) and this script's own topic from
the rollup.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import cap_dayahead as cd
import reserve_outlook as ro
import nemweb_common as nw

STATE_FILE = "recap_state.json"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# Shown as-is, one full block per push: rebid_reconciler runs once daily (nothing to pool),
# interconnector_monitor already debounces at the source (state-tracked "flagged" dict - only
# fires on a NEW crossing, not every interval it stays at/near limit), pasa_monitor only pushes
# on an actual >=100MW declared-availability change (own state-file debounce, same as above).
ASIS_DISCRETE_SOURCES = ["rebid_reconciler", "interconnector_monitor", "pasa_monitor"]
# Pooled across the whole overnight window into one summary line per DUID instead - both
# scripts check each 5-min interval independently with NO debounce, confirmed live (neither
# has any state suppressing a repeat push), so a genuinely volatile night could otherwise
# produce dozens of separate near-identical blocks, one per push.
POOLED_DISCRETE_SOURCES = ["customer_watcher", "scada_drop_monitor"]
# Net-effect filtering (skip a trigger that cleared within the window) only applies to
# sources that actually push an explicit "cleared" line - everything else, including any
# source added later that doesn't emit one, defaults to DISCRETE (always show) so nothing
# silently disappears just because a source doesn't have a real clear signal.
THRESHOLD_SOURCES = ["spot_spike"]
# cap_dayahead, predispatch_tracker, reserve_outlook and pasa_monitor are all live-checked
# each morning now (format_cap_section, format_predispatch_eventuated_section,
# format_reserve_outlook_section, format_units_out_today) rather than log-replayed - their
# own scripts only log when something changes/moves enough, so log-replay could show a
# days-old, sometimes partial-region snapshot. weather_outlook is still log-replay (its own
# push already happens same-morning, so it's never meaningfully stale) - trimmed to just
# today (format_weather_today_section) since its own push now covers a full 7 days, too big
# for the recap's byte budget on its own. Plus a live price outlook
# (format_price_outlook_section) that has no push-based source at all.
EXCLUDED_SOURCES = {"market_read", "notification_recap"}

# Lines from threshold sources that mean "back to normal" for whatever key
# the line is about - pairs up with an earlier trigger line for the same key
# to cancel both out. Matched against predispatch_tracker's "back under $300"
# and spot_spike's "price drop" wording; interconnector_monitor currently has
# no equivalent clear text (it only ever pushes while something is at/near
# limit), so its lines never pair off and always show as still-active.
CLEAR_PATTERN = re.compile(r"back under|price drop|no longer|cleared", re.IGNORECASE)


def overnight_window(now: datetime) -> datetime:
    """Start of the overnight window: 4pm previous day, or 4pm Friday on Mondays."""
    days_back = 3 if now.weekday() == 0 else 1
    start_day = now - timedelta(days=days_back)
    return start_day.replace(hour=16, minute=0, second=0, microsecond=0)


def region_key(line: str) -> str | None:
    """First ':'-delimited token of a body line, e.g. 'Murraylink (VIC-SA) star' or 'TAS1'."""
    m = re.match(r"\s*([^:]+):", line)
    if not m:
        return None
    return re.sub(r"\s*[★☆*]\s*$", "", m.group(1).strip())  # strip trailing star marker


def body_lines(entry: dict) -> list[str]:
    lines = entry.get("message", "").splitlines()
    # Drop the first line if it reads like a header (no ':' - a summary sentence, not a per-key row)
    return [ln for ln in lines[1:] if ln.strip()] if lines else []


def net_effect_lines(entries: list[dict]) -> list[tuple[str, str]]:
    """
    entries: notification_log rows for one threshold source, sorted or not.
    Returns [(timestamp_str, line), ...] for keys still active at the end of
    the window - anything that triggered and cleared within the window is
    dropped entirely.
    """
    rows = []
    for e in entries:
        for line in body_lines(e):
            key = region_key(line)
            if key is None:
                continue
            rows.append((e["ts"], key, bool(CLEAR_PATTERN.search(line)), line.strip()))
    rows.sort(key=lambda r: r[0])

    active: dict[str, tuple[str, str]] = {}
    for ts, key, is_clear, line in rows:
        if is_clear:
            active.pop(key, None)
        else:
            active[key] = (ts, line)
    return sorted(active.values(), key=lambda t: t[0])


def format_discrete_section(source: str, entries: list[dict]) -> list[str]:
    lines = [f"\n{source}:"]
    for e in entries:
        ts = datetime.fromisoformat(e["ts"]).strftime("%a %H:%M")
        lines.append(f"  [{ts}] {e.get('title') or ''}")
        for body in body_lines(e):
            lines.append(f"    {body.strip()}")
    return lines


# Per-DUID move/drop lines from customer_watcher.py / scada_drop_monitor.py, e.g.
# "  TALWA1 (Tallawarra) [NSW1, Gas]: 297 -> 358MW (+61MW)". Fuel is optional - historical log
# entries predate this session's fuel-type addition to customer_watcher.py, so older lines are
# "[NSW1]" only, not "[NSW1, Gas]".
INDIVIDUAL_MOVE_RE = re.compile(r"^\s*(.+?) \[([^,\]]+)(?:, ([^\]]+))?\]: ([\d.,-]+) -> ([\d.,-]+)MW")
# customer_watcher.py's aggregate wind/solar/battery lines, e.g.
# "  Wind: net +180MW this interval (34 unit(s))".
AGGREGATE_MOVE_RE = re.compile(r"^\s*(Wind|Solar|Battery): net ([+-]?[\d.,]+)MW this interval \((\d+) unit\(s\)\)")


def pool_discrete_moves(entries: list[dict]) -> list[str]:
    """
    Pools individual-DUID move/drop lines across every push in the overnight window into one
    summary line per DUID (first prev value seen -> last curr value seen, net change, how many
    times it moved) - customer_watcher.py and scada_drop_monitor.py both check each 5-min
    interval independently with no debounce, so this is what stands between a volatile night
    and dozens of near-identical blocks. Also pools customer_watcher's aggregate
    wind/solar/battery lines into one cumulative net-movement line per fuel type.
    """
    individual: dict[str, dict] = {}
    aggregate: dict[str, dict] = {}

    for e in sorted(entries, key=lambda x: x["ts"]):
        for line in e.get("message", "").splitlines():
            m = INDIVIDUAL_MOVE_RE.match(line)
            if m:
                label, region, fuel, prev_s, curr_s = m.groups()
                try:
                    prev_v = float(prev_s.replace(",", ""))
                    curr_v = float(curr_s.replace(",", ""))
                except ValueError:
                    continue
                key = label.strip()
                d = individual.setdefault(key, {"region": region.strip(), "fuel": (fuel or "?").strip(),
                                                  "first_prev": prev_v, "count": 0})
                d["last_curr"] = curr_v
                d["count"] += 1
                continue
            m2 = AGGREGATE_MOVE_RE.match(line)
            if m2:
                fuel, net_s, _units_s = m2.groups()
                try:
                    net_v = float(net_s.replace(",", ""))
                except ValueError:
                    continue
                d = aggregate.setdefault(fuel, {"net_sum": 0.0, "count": 0})
                d["net_sum"] += net_v
                d["count"] += 1

    lines: list[str] = []
    for label in sorted(individual, key=lambda k: -abs(individual[k]["last_curr"] - individual[k]["first_prev"])):
        d = individual[label]
        net = d["last_curr"] - d["first_prev"]
        sign = "+" if net >= 0 else ""
        move_str = f"{d['count']} move(s), " if d["count"] > 1 else ""
        bracket = f"{d['region']}, {d['fuel']}" if d["fuel"] != "?" else d["region"]
        lines.append(f"  {label} [{bracket}]: {move_str}{d['first_prev']:.0f} -> {d['last_curr']:.0f}MW (net {sign}{net:.0f}MW)")

    if aggregate:
        for fuel in sorted(aggregate):
            d = aggregate[fuel]
            sign = "+" if d["net_sum"] >= 0 else ""
            lines.append(f"  {fuel}: {d['count']} interval(s), cumulative net {sign}{d['net_sum']:.0f}MW")

    return lines


def format_pooled_discrete_section(source: str, entries: list[dict]) -> list[str]:
    if not entries:
        return []
    lines = [f"\n{source} ({len(entries)} interval(s) overnight):"]
    pooled = pool_discrete_moves(entries)
    lines.extend(pooled if pooled else ["  (no parseable moves)"])
    return lines


def format_threshold_section(source: str, entries: list[dict]) -> list[str]:
    still_active = net_effect_lines(entries)
    if not still_active:
        return []
    lines = [f"\n{source} (still active at cutoff):"]
    for ts, line in still_active:
        time_str = datetime.fromisoformat(ts).strftime("%a %H:%M")
        lines.append(f"  [{time_str}] {line.strip()}")
    return lines


def format_reserve_outlook_section(now: datetime) -> list[str]:
    """
    Live check, not log-replay - reserve_outlook.py only pushes when something changed, so its
    log could be showing a days-old reading. Fetches the same STPASA REGIONSOLUTION snapshot
    fresh instead. One line per region: 'adequate reserve <first day> to <last day>' covering
    the full outlook window, plus any specific day(s) with an LOR condition flagged (AEMO's own
    description) called out as an exception.
    """
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    lines = ["\nreserve_outlook:"]
    try:
        files = nw.get_latest_files(ro.STPASA_URL, ro.STPASA_PATTERN, n=1)
        df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(files[-1])), "REGIONSOLUTION")
        df["_interval_dt"] = df["INTERVAL_DATETIME"].apply(ro.parse_interval_datetime)
        df["SURPLUSRESERVE"] = pd.to_numeric(df["SURPLUSRESERVE"], errors="coerce")
        df["LORCONDITION"] = pd.to_numeric(df["LORCONDITION"], errors="coerce").fillna(0)
        df["_day"] = df["_interval_dt"].dt.date
    except Exception as exc:
        lines.append(f"  Could not fetch live data ({exc}).")
        return lines

    for region in regions:
        region_df = df[df["REGIONID"] == region]
        if region_df.empty:
            lines.append(f"  {region}: no data")
            continue
        days = sorted(region_df["_day"].unique())
        span = days[0].isoformat() if days[0] == days[-1] else f"{days[0].isoformat()} to {days[-1].isoformat()}"
        flags = []
        for day, day_df in region_df.groupby("_day"):
            lor_level = int(day_df["LORCONDITION"].max())
            if lor_level > 0:
                desc = ro.LOR_DESCRIPTIONS.get(lor_level, f"LOR condition {lor_level} forecast")
                flags.append(f"{day}: {desc}")
        if flags:
            lines.append(f"  {region}: adequate reserve {span}, EXCEPT {'; '.join(flags)}")
        else:
            lines.append(f"  {region}: adequate reserve {span}")
    return lines


NETWORK_OUTAGE_RECAP_STATE_FILE = "recap_network_outage_state.json"


def format_network_outage_changes_overnight(now: datetime) -> list[str]:
    """
    NEW/CHANGED network outage announcements overnight, across ALL transmission assets - not
    just the 6 interconnectors interconnector_monitor.py already checks in its own OVERNIGHT
    entry. Same NEW/CHANGED debounce pattern (compare current status per outage key against
    last-seen), own state file since this is scoped to the recap's own daily cadence, not
    interconnector_monitor's 5-min one - the underlying HIO feed only updates weekly anyway.
    """
    try:
        df = nw.fetch_high_impact_outages()
    except Exception as exc:
        return [f"\nNetwork outage changes overnight (all transmission assets): could not fetch ({exc})."]

    seen = nw.read_state(NETWORK_OUTAGE_RECAP_STATE_FILE, default=None)
    first_run = seen is None
    seen = seen or {}

    current: dict[str, str] = {}
    changes = []
    for _, row in df.iterrows():
        key = f"{row.get('Network Asset')}|{row.get('Start')}|{row.get('Finish')}"
        status = str(row.get("Status") or "")
        current[key] = status
        if first_run:
            continue
        prev_status = seen.get(key)
        asset = row.get("Network Asset") or "?"
        region = row.get("Region") or "?"
        if prev_status is None:
            changes.append(f"  NEW: {asset} [{region}]: {row.get('Start')} to {row.get('Finish', '?')} ({status})")
        elif prev_status != status:
            changes.append(f"  CHANGED: {asset} [{region}]: {row.get('Start')} to {row.get('Finish', '?')} - now {status} (was {prev_status})")

    nw.write_state(NETWORK_OUTAGE_RECAP_STATE_FILE, current)

    lines = ["\nNetwork outage changes overnight (all transmission assets):"]
    if first_run:
        lines.append("  (first run - establishing baseline, nothing to compare against yet)")
    elif not changes:
        lines.append("  None.")
    else:
        lines.extend(changes)
    return lines


def format_units_out_today(now: datetime) -> list[str]:
    """
    Live-enough without re-fetching the raw MTPASA data: pasa_monitor.py maintains a rolling
    cache (pasa_recent_windows.json) of every declared availability change it's found,
    specifically so downstream consumers don't need to re-download/re-diff the ~250k-row MTPASA
    snapshot themselves - re-fetching that here would just duplicate pasa_monitor's own job.
    Filters the cache to windows covering today with a real reduction (delta < 0) - "what units
    are out today", not "what changed recently" (the old log-replay's framing).
    """
    cache = nw.read_state("pasa_recent_windows.json", default=[])
    today_str = now.date().isoformat()
    active = sorted(
        (w for w in cache if w.get("start", "") <= today_str <= w.get("end", "") and w.get("delta", 0) < 0),
        key=lambda w: w["delta"],
    )
    lines = ["\nUnits out today (>=100MW declared reduction):"]
    if not active:
        lines.append("  None.")
        return lines
    for w in active:
        name = w.get("station") or w["duid"]
        owner = w.get("owner") or "UNKNOWN"
        lines.append(f"  {w['duid']} ({name}) [{owner}, {w['region']}]: {w['delta']:.0f}MW until {w['end']}")
    return lines


LARGE_OUTAGE_THRESHOLD_MW = 300
LARGE_OUTAGE_LOOKAHEAD_DAYS = 42  # 6 weeks


def format_large_outages_upcoming(now: datetime) -> list[str]:
    """
    Same pasa_recent_windows.json cache as units-out-today, but widened: any declared
    reduction >=300MW - the QED-established size range for single-unit price-moving events
    (every unit AEMO's own quarterly reports ever named as price-moving was 120-760MW,
    always coal/gas/hydro, never a single wind/solar/battery unit) - whose window is still
    active or starts within the next 6 weeks, not just today.
    """
    cache = nw.read_state("pasa_recent_windows.json", default=[])
    today = now.date()
    cutoff = today + timedelta(days=LARGE_OUTAGE_LOOKAHEAD_DAYS)
    upcoming = sorted(
        (w for w in cache
         if w.get("delta", 0) <= -LARGE_OUTAGE_THRESHOLD_MW
         and datetime.strptime(w["start"], "%Y-%m-%d").date() <= cutoff
         and datetime.strptime(w["end"], "%Y-%m-%d").date() >= today),
        key=lambda w: w["start"],
    )
    lines = [f"\nLarge outages (>={LARGE_OUTAGE_THRESHOLD_MW}MW) in the next {LARGE_OUTAGE_LOOKAHEAD_DAYS // 7} weeks:"]
    if not upcoming:
        lines.append("  None.")
        return lines
    for w in upcoming:
        name = w.get("station") or w["duid"]
        owner = w.get("owner") or "UNKNOWN"
        lines.append(f"  {w['duid']} ({name}) [{owner}, {w['region']}]: {w['delta']:.0f}MW, {w['start']} to {w['end']}")
    return lines


def format_network_outages_today(now: datetime) -> list[str]:
    """
    Live check against AEMO's High Impact Outages feed (planned transmission/network outages -
    NOT generators, which units_out_today/format_large_outages_upcoming already cover). Unlike
    interconnector_monitor.py's own OVERNIGHT alert (only the 6 tracked interconnectors, only
    NEW/CHANGED announcements), this covers ANY transmission asset - interconnectors and
    everything else - filtered to whatever's actually active today.
    """
    try:
        df = nw.fetch_high_impact_outages()
    except Exception as exc:
        return [f"\nNetwork outages today: could not fetch ({exc})."]

    today = now.date()
    active = []
    for _, row in df.iterrows():
        try:
            start_dt = datetime.strptime(row["Start"], "%d/%m/%Y %H:%M").date()
            finish_dt = datetime.strptime(row["Finish"], "%d/%m/%Y %H:%M").date()
        except (ValueError, TypeError, KeyError):
            continue
        if start_dt <= today <= finish_dt:
            active.append((start_dt, row))

    lines = ["\nNetwork outages today (interconnectors + other transmission assets):"]
    if not active:
        lines.append("  None.")
        return lines
    for _, row in sorted(active, key=lambda t: t[0]):
        asset = row.get("Network Asset") or "?"
        region = row.get("Region") or "?"
        nsp = row.get("NSP") or "?"
        status = row.get("Status") or "?"
        lines.append(f"  {asset} [{region}, {nsp}]: {row['Start']} to {row.get('Finish', '?')} ({status})")
    return lines


def format_major_network_outages_upcoming(now: datetime) -> list[str]:
    """
    Same High Impact Outages feed as units-out-today's network equivalent, widened like
    format_large_outages_upcoming does for generators: anything flagged Inter-Regional
    (AEMO's own "T" flag, confirmed live: 76 of 206 current rows) - i.e. genuinely major,
    cross-region-significant, not just a local asset - whose window is still active or starts
    within the next 6 weeks.
    """
    try:
        df = nw.fetch_high_impact_outages()
    except Exception as exc:
        return [f"\nMajor network outages upcoming: could not fetch ({exc})."]

    today = now.date()
    cutoff = today + timedelta(days=LARGE_OUTAGE_LOOKAHEAD_DAYS)
    major = []
    for _, row in df.iterrows():
        if str(row.get("Inter-Regional") or "").strip().upper() != "T":
            continue
        try:
            start_dt = datetime.strptime(row["Start"], "%d/%m/%Y %H:%M").date()
            finish_dt = datetime.strptime(row["Finish"], "%d/%m/%Y %H:%M").date()
        except (ValueError, TypeError, KeyError):
            continue
        if start_dt <= cutoff and finish_dt >= today:
            major.append((start_dt, finish_dt, row))

    lines = [f"\nMajor network outages (Inter-Regional) in the next {LARGE_OUTAGE_LOOKAHEAD_DAYS // 7} weeks:"]
    if not major:
        lines.append("  None.")
        return lines

    # Pool by asset - the same line (e.g. recurring overnight maintenance windows) can appear
    # 5+ times as separate rows, which would blow the recap's byte budget listed individually.
    # One summary line per asset instead: how many windows, spanning what range.
    by_asset: dict[str, list[tuple]] = {}
    for start_dt, finish_dt, row in major:
        key = row.get("Network Asset") or "?"
        by_asset.setdefault(key, []).append((start_dt, finish_dt, row))

    for asset in sorted(by_asset, key=lambda a: by_asset[a][0][0]):
        rows = sorted(by_asset[asset], key=lambda t: t[0])
        first_row = rows[0][2]
        region = first_row.get("Region") or "?"
        nsp = first_row.get("NSP") or "?"
        earliest = min(r[0] for r in rows)
        latest = max(r[1] for r in rows)
        count_str = f"{len(rows)} window(s), " if len(rows) > 1 else ""
        lines.append(f"  {asset} [{region}, {nsp}]: {count_str}{earliest.strftime('%d-%b')} to {latest.strftime('%d-%b')}")
    return lines


def format_weather_today_section(latest: dict | None) -> list[str]:
    """
    weather_outlook.py's own push now covers a full 7 days per region (per your request) -
    too big for the bundled recap's byte budget on its own (2200+ bytes alone). Trims the
    logged push down to just each region's first day (today/tomorrow, whichever the push's
    first row is), one line per region. The full week is still available via the standalone
    weather push, unchanged.
    """
    if latest is None:
        return ["\nweather_outlook: no data available."]
    lines = ["\nweather_outlook:"]
    current_header = None
    day_shown = False
    for ln in latest.get("message", "").splitlines()[1:]:  # skip the top summary line
        if not ln.strip():
            continue
        if not ln.startswith(" "):
            current_header = ln.rstrip(":").strip()
            day_shown = False
            continue
        if current_header and not day_shown:
            lines.append(f"  {current_header}: {ln.strip()}")
            day_shown = True
    return lines


def format_price_outlook_section(now: datetime) -> list[str]:
    """Live check: today's remaining forecast price range per region, from the same Predispatch
    run cap_dayahead/predispatch use - "what prices are expected to do" for the rest of today,
    which nothing else in this report actually states plainly."""
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    now_naive = now.replace(tzinfo=None)
    midnight_tonight = datetime(now_naive.year, now_naive.month, now_naive.day) + timedelta(days=1)

    lines = ["\nPrice outlook (rest of today, live forecast):"]
    try:
        pd_files = nw.get_latest_files(cd.PREDISPATCH_URL, cd.PREDISPATCH_PATTERN, n=1)
        pd_df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(pd_files[-1])), "PDREGION")
        pd_df["RRP"] = pd.to_numeric(pd_df["RRP"], errors="coerce")
        pd_df["_period_dt"] = pd_df["PERIODID"].apply(cd.parse_price_datetime)
    except Exception as exc:
        lines.append(f"  Could not fetch live forecast ({exc}).")
        return lines

    for region in regions:
        region_pd = pd_df[pd_df["REGIONID"] == region]
        remaining = cd.windowed_prices(region_pd, now_naive, midnight_tonight) if not region_pd.empty else pd.Series(dtype=float)
        if remaining.empty:
            lines.append(f"  {region}: no forecast data available")
            continue
        lines.append(f"  {region}: ${remaining.min():,.0f}-${remaining.max():,.0f}/MWh (avg ${remaining.mean():,.0f})")
    return lines


GAS_SPREAD_WATCH_THRESHOLD = 15.0


def format_gas_section(latest: dict | None) -> list[str]:
    """
    Straight log-replay of gas_spread_tracker's own push - it reports daily unconditionally, no
    threshold gate, so it's never meaningfully stale (same treatment as weather_outlook), unlike
    cap/predispatch/reserve/pasa which only push on a qualifying change. Adds the QED-grounded
    watch level on top: causal_rules.py's rule_gas_spread already established $15/GJ as the
    threshold - your own QED dataset shows a $25-40/GJ spread building for TWO QUARTERS before
    domestic gas actually caught up to LNG parity ($28.40/GJ) in Q2 2022, and $15/GJ is low
    enough to catch that buildup phase while staying above ordinary day-to-day noise.
    """
    if latest is None:
        return ["\ngas_spread: no data available."]
    lines = ["\ngas_spread:"]
    lines.extend(f"  {ln.strip()}" for ln in body_lines(latest) if ln.strip())

    state = nw.read_state("gas_spread_state.json", default={})
    current_spread = state.get("spread_aud_gj")
    if current_spread is not None and abs(current_spread) >= GAS_SPREAD_WATCH_THRESHOLD:
        status = f"ABOVE - currently ${current_spread:+.2f}/GJ"
    elif current_spread is not None:
        status = f"below - currently ${current_spread:+.2f}/GJ"
    else:
        status = "unknown - no current spread figure available"
    lines.append(
        f"  Watch level: spread >= ${GAS_SPREAD_WATCH_THRESHOLD:.0f}/GJ ({status}). QED history: "
        f"2021-22 crisis had a $25-40/GJ spread build for 2 quarters before domestic gas hit LNG "
        f"parity at $28.40/GJ."
    )
    return lines



PERIOD_SPAN_RE = re.compile(r"from (\d{2}:\d{2}) to (\d{2}:\d{2}) NEM time")
# Pre-pooling log format ("NSW1: $308/MWh forecast for 20:00 NEM time" / "back under $300
# (...) for 18:00 NEM time") - a single 30-min period, not a range. Older log entries predate
# this session's range-pooling change to predispatch_tracker.py, so both formats need support
# until enough time passes that only the new one remains in the log.
PERIOD_SINGLE_RE = re.compile(r"for (\d{2}:\d{2}) NEM time")


def extract_predispatch_forecast_ranges(entries: list[dict], now: datetime) -> list[dict]:
    """
    From predispatch_tracker's own logged lines ("NSW1: above $300 from 20:00 to 22:00 NEM
    time" / "revised to ... from ... NEM time") - pulls out (region, start, end) ranges that
    were forecast above threshold. "back under" (cleared) lines are excluded - that's not a
    forecast-above-threshold claim. Only ranges that have fully elapsed by `now` are returned,
    since a still-future range has nothing to check against actuals yet.
    """
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    ranges = []
    for e in entries:
        push_dt = datetime.fromisoformat(e["ts"]).replace(tzinfo=None)
        for line in body_lines(e):
            if "back under" in line.lower():
                continue
            m_region = re.match(r"\s*([A-Za-z0-9]+):", line)
            if not m_region:
                continue
            region = m_region.group(1)

            m_span = PERIOD_SPAN_RE.search(line)
            if m_span:
                start_t, end_t = m_span.group(1), m_span.group(2)
            else:
                m_single = PERIOD_SINGLE_RE.search(line)
                if not m_single:
                    continue
                start_t = m_single.group(1)
                end_t = (datetime.strptime(start_t, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")

            start_dt = datetime.combine(push_dt.date(), datetime.strptime(start_t, "%H:%M").time())
            end_dt = datetime.combine(push_dt.date(), datetime.strptime(end_t, "%H:%M").time())
            # A period can be logged shortly before it starts (push near midnight, period just
            # after) - if naively anchoring to the push's own calendar date puts the period
            # implausibly far in the past relative to the push, it actually belongs the next day.
            if start_dt < push_dt - timedelta(hours=6):
                start_dt += timedelta(days=1)
                end_dt += timedelta(days=1)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)  # crosses midnight
            ranges.append({"region": region, "start": start_dt, "end": end_dt})
    return [r for r in ranges if r["end"] <= now_naive]


def fetch_actual_prices_for_ranges(ranges: list[dict]) -> pd.DataFrame:
    """Actual 5-min DispatchIS prices covering only the union of the given ranges - not the
    whole overnight window, to keep this to a handful of downloads rather than hundreds."""
    if not ranges:
        return pd.DataFrame(columns=["REGIONID", "RRP", "_dt"])
    window_start = min(r["start"] for r in ranges)
    window_end = max(r["end"] for r in ranges)

    files = nw.list_nemweb_files(cd.DISPATCHIS_URL, cd.DISPATCHIS_PATTERN)
    needed = []
    for url in files:
        m = re.search(r"PUBLIC_DISPATCHIS_(\d{12})_", url)
        if not m:
            continue
        file_dt = datetime.strptime(m.group(1), "%Y%m%d%H%M")
        if window_start - timedelta(minutes=5) <= file_dt <= window_end:
            needed.append(url)

    rows = []
    for url in needed:
        try:
            table = nw.download_and_get_table(url, "DISPATCHPRICE")
        except Exception as exc:
            print(f"[notification_recap] WARNING: skipping {url}: {exc}")
            continue
        table["RRP"] = pd.to_numeric(table["RRP"], errors="coerce")
        table["_dt"] = table["SETTLEMENTDATE"].apply(cd.parse_price_datetime)
        rows.append(table[["REGIONID", "RRP", "_dt"]])
    if not rows:
        return pd.DataFrame(columns=["REGIONID", "RRP", "_dt"])
    return pd.concat(rows, ignore_index=True)


def format_cap_overnight_section(now: datetime, since: datetime) -> list[str]:
    """Did a cap actually pay out overnight, from real actuals (not the day-ahead forecast) -
    same ASX settlement formula (nw.cap_settlement) cap_dayahead.py uses, reusing the same
    file-fetch helper as the predispatch eventuate check."""
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    strike = cfg.get("cap_strike", 300)
    since_naive = since.replace(tzinfo=None) if since.tzinfo else since
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now

    lines = ["\nCap payouts overnight:"]
    try:
        actuals = fetch_actual_prices_for_ranges([{"start": since_naive, "end": now_naive}])
    except Exception as exc:
        lines.append(f"  Could not fetch actuals ({exc}).")
        return lines

    payouts = []
    for region in regions:
        prices = actuals.loc[actuals["REGIONID"] == region, "RRP"] if not actuals.empty else []
        if len(prices) == 0:
            continue
        _, payout_full = nw.cap_settlement(prices, strike, interval_hours=5 / 60)
        payout = payout_full / 24
        if payout > 0:
            payouts.append(f"  {region}: ${payout:,.2f}")

    if payouts:
        lines.append("  PAID OUT:")
        lines.extend(payouts)
    else:
        lines.append("  No region paid out overnight.")
    return lines


def format_predispatch_eventuated_section(now: datetime, log_entries: list[dict]) -> list[str]:
    """
    Bundled per-region check: for every period predispatch_tracker forecast above threshold
    overnight (and which has since elapsed), did the actual price ever exceed threshold during
    that period? Belongs in OVERNIGHT (a retrospective check against what already happened) -
    not DAY AHEAD, which is format_predispatch_forecast_section below (a genuine forward-looking
    forecast, no comparison against actuals).
    """
    cfg = nw.CONFIG
    threshold = cfg.get("predispatch_alert_threshold", 300)
    since = overnight_window(now)

    entries = [
        e for e in log_entries
        if e.get("source") == "predispatch_tracker"
        and since <= datetime.fromisoformat(e["ts"]) <= now
    ]
    ranges = extract_predispatch_forecast_ranges(entries, now)

    lines = [f"\npredispatch_tracker - forecast vs actual (>${threshold}):"]
    if not ranges:
        lines.append(f"  No ${threshold}+ forecasts made overnight.")
        return lines

    try:
        actuals = fetch_actual_prices_for_ranges(ranges)
    except Exception as exc:
        lines.append(f"  Could not check actuals ({exc}).")
        return lines

    by_region: dict[str, list[dict]] = {}
    for r in ranges:
        by_region.setdefault(r["region"], []).append(r)

    for region in sorted(by_region):
        region_actuals = actuals[actuals["REGIONID"] == region] if not actuals.empty else actuals
        peak = None
        for r in by_region[region]:
            mask = (region_actuals["_dt"] >= r["start"]) & (region_actuals["_dt"] < r["end"])
            sub = region_actuals[mask]
            if not sub.empty:
                m = sub["RRP"].max()
                peak = m if peak is None else max(peak, m)
        if peak is None:
            lines.append(f"  {region}: prices originally expected to be above ${threshold} - couldn't verify (no actual data found)")
        elif peak > threshold:
            lines.append(f"  {region}: prices originally expected to be above ${threshold} - did eventuate (actual peaked at ${peak:,.0f})")
        else:
            lines.append(f"  {region}: prices originally expected to be above ${threshold} - didn't eventuate (actual peaked at ${peak:,.0f})")

    return lines


def format_predispatch_forecast_section(now: datetime) -> list[str]:
    """
    Live, forward-looking only - day ahead, not day before. Fetches the current Predispatch
    run fresh and bundles any upcoming periods forecast above threshold into per-region ranges,
    same shape predispatch_tracker.py's own pooled alert uses. Replaced an earlier "did last
    night's forecast eventuate" version - you clarified this belongs in DAY AHEAD as a genuine
    forecast, not a retrospective check against what already happened.
    """
    cfg = nw.CONFIG
    threshold = cfg.get("predispatch_alert_threshold", 300)
    now_naive = now.replace(tzinfo=None)

    lines = [f"\nPredispatch price forecast (>${threshold}):"]
    try:
        pd_files = nw.get_latest_files(cd.PREDISPATCH_URL, cd.PREDISPATCH_PATTERN, n=1)
        pd_df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(pd_files[-1])), "PDREGION")
        pd_df["RRP"] = pd.to_numeric(pd_df["RRP"], errors="coerce")
        pd_df["_period_dt"] = pd_df["PERIODID"].apply(cd.parse_price_datetime)
    except Exception as exc:
        lines.append(f"  Could not fetch live forecast ({exc}).")
        return lines

    above = pd_df[(pd_df["_period_dt"] >= now_naive) & (pd_df["RRP"] > threshold)]
    if above.empty:
        lines.append(f"  No periods forecast above ${threshold}.")
        return lines

    by_region: dict[str, list[tuple[datetime, float]]] = {}
    for _, row in above.sort_values(["REGIONID", "_period_dt"]).iterrows():
        by_region.setdefault(row["REGIONID"], []).append((row["_period_dt"], row["RRP"]))

    for region in sorted(by_region):
        entries = by_region[region]
        run = [entries[0]]
        ranges = []
        for entry in entries[1:]:
            if entry[0] - run[-1][0] == timedelta(minutes=30):
                run.append(entry)
            else:
                ranges.append(run)
                run = [entry]
        ranges.append(run)

        for run in ranges:
            start = run[0][0]
            end = run[-1][0] + timedelta(minutes=30)
            prices = [p for _, p in run]
            price_str = f"${min(prices):,.0f}" if min(prices) == max(prices) else f"${min(prices):,.0f}-${max(prices):,.0f}"
            lines.append(f"  {region}: forecast above ${threshold} from {start.strftime('%H:%M')} to {end.strftime('%H:%M')} NEM time ({price_str}/MWh)")

    return lines


def format_cap_section(now: datetime) -> list[str]:
    """
    Live cap-payout check, not log-replay - cap_dayahead.py only logs when a region's own
    figure moves >=10%, so replaying its log gives a stale, partial-region snapshot (confirmed
    live: a 6-day-old push showing only VIC1/SA1/TAS1, NSW1/QLD1 silently absent because they
    hadn't moved on that particular day). This does its own check each morning instead:
    quarter-to-date for all 5 regions (read straight from cap_quarter_state.json - already
    settled data, no fetch needed) and a fresh today/tomorrow fetch, flagging a region only if
    it's actually paying out.
    """
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    strike = cfg.get("cap_strike", 300)
    now_naive = now.replace(tzinfo=None)

    lines = ["\nCap payouts:"]

    q_start, q_end, q_label = nw.quarter_bounds(now_naive)
    total_quarter_days = (q_end - q_start).days
    qstate = nw.read_state("cap_quarter_state.json", default=None)
    if qstate and qstate.get("quarter_label") == q_label:
        lines.append(f"  Quarter-to-date ({q_label}, settled through {qstate.get('last_day_included', '?')}):")
        for region in regions:
            _, qtd_full = nw.cap_settlement(qstate["prices"].get(region, []), strike, interval_hours=5 / 60)
            qtd = (qtd_full / 24) / total_quarter_days
            lines.append(f"    {region}: ${qtd:,.2f}")
    else:
        lines.append(f"  No settled quarter-to-date baseline yet for {q_label}.")

    try:
        midnight_tonight = datetime(now_naive.year, now_naive.month, now_naive.day) + timedelta(days=1)
        midnight_day_after = midnight_tonight + timedelta(days=1)

        actual_prices = cd.actual_prices_since_midnight(regions, now_naive)
        pd_files = nw.get_latest_files(cd.PREDISPATCH_URL, cd.PREDISPATCH_PATTERN, n=1)
        pd_df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(pd_files[-1])), "PDREGION")
        pd_df["RRP"] = pd.to_numeric(pd_df["RRP"], errors="coerce")
        pd_df["_period_dt"] = pd_df["PERIODID"].apply(cd.parse_price_datetime)

        payouts = []
        for region in regions:
            region_actual = actual_prices.get(region, [])
            _, actual_payout = nw.cap_settlement(region_actual, strike, interval_hours=5 / 60)
            region_pd = pd_df[pd_df["REGIONID"] == region]
            remainder_prices = cd.windowed_prices(region_pd, now_naive, midnight_tonight) if not region_pd.empty else pd.Series(dtype=float)
            _, remainder_payout = nw.cap_settlement(remainder_prices, strike, interval_hours=cd.PREDISPATCH_PERIOD_HOURS)
            today_payout = (actual_payout + remainder_payout) / 24

            tomorrow_prices = cd.windowed_prices(region_pd, midnight_tonight, midnight_day_after) if not region_pd.empty else pd.Series(dtype=float)
            _, tomorrow_payout_full = nw.cap_settlement(tomorrow_prices, strike, interval_hours=cd.PREDISPATCH_PERIOD_HOURS)
            tomorrow_payout = tomorrow_payout_full / 24

            if today_payout > 0 or tomorrow_payout > 0:
                payouts.append(f"    {region}: today ${today_payout:,.2f}, tomorrow ${tomorrow_payout:,.2f}")

        lines.append("  Today/tomorrow:")
        if payouts:
            lines.append("    PAYING OUT:")
            lines.extend(payouts)
        else:
            lines.append("    No region paying out today or tomorrow.")
    except Exception as exc:
        lines.append(f"  Today/tomorrow: could not fetch live data ({exc})")

    return lines


def build_recap(now: datetime, log_entries: list[dict]) -> str:
    since = overnight_window(now)
    is_monday = now.weekday() == 0
    span_desc = "since Friday" if is_monday else "overnight"

    window_entries = [
        e for e in log_entries
        if e.get("source") not in EXCLUDED_SOURCES
        and since <= datetime.fromisoformat(e["ts"]) <= now
    ]
    by_source: dict[str, list[dict]] = {}
    for e in window_entries:
        by_source.setdefault(e["source"], []).append(e)

    lines = [f"Morning Recap: {span_desc} ({since.strftime('%a %d-%b %H:%M')} to {now.strftime('%a %d-%b %H:%M')} NEM time)"]

    lines.append("\n=== OVERNIGHT - what happened ===")
    overnight_had_content = False
    for source in ASIS_DISCRETE_SOURCES:
        if source in by_source:
            overnight_had_content = True
            lines.extend(format_discrete_section(source, by_source[source]))
    for source in POOLED_DISCRETE_SOURCES:
        if source in by_source:
            overnight_had_content = True
            lines.extend(format_pooled_discrete_section(source, by_source[source]))
    for source in THRESHOLD_SOURCES:
        section = format_threshold_section(source, by_source.get(source, []))
        if section:
            overnight_had_content = True
            lines.extend(section)
    if not overnight_had_content:
        lines.append("\nQuiet - nothing to report.")
    lines.extend(format_cap_overnight_section(now, since))
    lines.extend(format_network_outage_changes_overnight(now))
    lines.extend(format_predispatch_eventuated_section(now, log_entries))

    lines.append("\n\n=== DAY AHEAD - what's coming ===")
    lines.extend(format_cap_section(now))
    lines.extend(format_predispatch_forecast_section(now))
    lines.extend(format_price_outlook_section(now))
    lines.extend(format_reserve_outlook_section(now))
    lines.extend(format_units_out_today(now))
    lines.extend(format_large_outages_upcoming(now))
    lines.extend(format_network_outages_today(now))
    lines.extend(format_major_network_outages_upcoming(now))

    latest_gas = None
    for e in log_entries:
        if e["source"] == "gas_spread_tracker" and (latest_gas is None or e["ts"] > latest_gas["ts"]):
            latest_gas = e
    lines.extend(format_gas_section(latest_gas))

    latest_weather = None
    for e in log_entries:
        if e["source"] == "weather_outlook" and (latest_weather is None or e["ts"] > latest_weather["ts"]):
            latest_weather = e
    lines.extend(format_weather_today_section(latest_weather))

    return "\n".join(lines)


def build_html_page(title: str, body_text: str) -> str:
    """
    Wraps the recap's plain text as a minimal HTML page for the ntfy attachment. HTML rather
    than .txt specifically because a phone opens it straight in its browser with no dependency
    on a text-file app being installed/registered - you flagged that .txt attachments have
    given you trouble opening on mobile before.
    """
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;"
        "word-wrap:break-word;padding:16px;font-size:15px;line-height:1.4;"
        "max-width:700px;margin:0 auto;}</style>"
        f"</head><body>{html.escape(body_text)}</body></html>"
    )


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("recap", "nem-recap")

    now = datetime.now(SYDNEY_TZ)
    if now.weekday() >= 5:
        print("[notification_recap] Weekend - no recap.")
        return

    # This workflow has two independent triggers (repository_dispatch + a schedule: fallback)
    # that can both genuinely fire the same morning - confirmed live, 2026-09-10 sent two real
    # pushes ~2h apart (07:34 via dispatch, 09:26 via the fallback schedule). Unlike the
    # higher-frequency workflows, a once-a-day job has no natural gap between the two triggers
    # that would make a double-fire rare, so the script itself has to refuse a second run today.
    prior_state = nw.read_state(STATE_FILE, default={})
    last_recap_at = prior_state.get("last_recap_at")
    if last_recap_at and datetime.fromisoformat(last_recap_at).astimezone(SYDNEY_TZ).date() == now.date():
        print(f"[notification_recap] Already sent today's recap at {last_recap_at} - skipping duplicate run.")
        return

    log_entries = nw.read_notification_log(since=now - timedelta(days=10))
    message = build_recap(now, log_entries)
    print(message)

    title = "Morning Recap" if now.weekday() != 0 else "Morning Recap (since Friday)"
    # Full content goes out as an HTML attachment instead of the ntfy message body - this
    # recap regularly runs well over ntfy's ~4096-byte body limit once every section is live,
    # and you explicitly don't want anything trimmed to fit. push_ntfy's own truncation guard
    # would otherwise silently cut it.
    html_page = build_html_page(title, message)
    short_message = f"Full recap ready - tap to open ({len(message.encode('utf-8')):,} bytes)"

    nw.push_ntfy_attachment(
        topic=topic,
        filename=f"recap-{now.strftime('%Y-%m-%d')}.html",
        content=html_page,
        short_message=short_message,
        title=title,
        tags=["sunrise"],
    )

    nw.write_state(STATE_FILE, {"last_recap_at": now.isoformat()})


if __name__ == "__main__":
    main()
