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
import coal_fleet_trend as cft
import negative_pricing_tracker as npt
import reserve_outlook as ro
import nemweb_common as nw

STATE_FILE = "recap_state.json"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# Shown as-is, one full block per push: rebid_reconciler runs once daily (nothing to pool),
# pasa_monitor only pushes on an actual >=100MW declared-availability change (own state-file
# debounce). interconnector_monitor is handled separately (format_pooled_interconnector_section)
# - it debounces per-crossing at the source, but a sustained multi-hour bind can still repeat
# across many pushes with no clear/hysteresis pairing available, and its own planned-outage
# lines got pooled AND dropped entirely once format_network_outage_changes_overnight started
# covering all transmission assets (confirmed live: a 29-line dump, a strict subset of the
# broader section - keeping both was pure duplication).
ASIS_DISCRETE_SOURCES = ["rebid_reconciler", "pasa_monitor"]
# customer_watcher + scada_drop_monitor are merged into one per-unit section instead
# (format_unit_events_overnight) - both scripts check each 5-min interval independently with
# NO debounce (a volatile night could otherwise produce dozens of near-identical blocks), and
# scada_drop_monitor's own coverage is a strict subset of customer_watcher's (same fuel types,
# drops only), so showing them separately was pure duplication of the same real events.
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


DROP_REASON_RE = re.compile(r"^ {4}\[(\w+)/(\w+)\] (.+)$")
DUID_DROP_RE = re.compile(r"^ {2}(\S.*?) - drop\(s\) at")


def format_drop_reasons_overnight(entries: list[dict]) -> list[str]:
    """
    For every drop rebid_reconciler actually reconciled, states plainly whether it was a
    genuine trip or something else - you asked directly "was it a trip or not", which the raw
    [TAG/TYPE] shorthand buried in the full rebid_reconciler block doesn't answer at a glance.
    Uses rebid_reconciler's own keyword-tagged classification: FORCED (its explanation text
    matched forced/trip/fault/failure/unplanned/breaker/boiler/loss of/tube leak/emergency/
    protection) becomes "TRIPPED"; ECONOMIC or OTHER becomes "not a trip" plus the real reason.
    """
    reasons = []
    for e in entries:
        owner = None
        duid_label = None
        for line in e.get("message", "").splitlines():
            if not line.strip():
                continue
            if not line.startswith(" "):
                owner = line.strip().rstrip(":")
                continue
            m = DUID_DROP_RE.match(line)
            if m:
                duid_label = m.group(1)
                continue
            m = DROP_REASON_RE.match(line)
            if m and duid_label:
                tag, _entrytype, explanation = m.groups()
                verdict = "TRIPPED" if tag == "FORCED" else "not a trip"
                reasons.append(f"  {duid_label} [{owner or 'UNKNOWN'}]: {verdict} - {explanation}")

    if not reasons:
        return []
    lines = ["\nDrop reasons overnight (tripped or not):"]
    lines.extend(reasons)
    return lines


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


def build_trip_verdict_lookup(rebid_entries: list[dict]) -> dict[str, str]:
    """DUID code -> 'TRIPPED: <reason>' / 'not a trip: <reason>', from rebid_reconciler's own
    FORCED/ECONOMIC/OTHER classification. Shared by format_drop_reasons_overnight (its own
    section) and format_unit_events_overnight (inline annotation on the matching unit line)."""
    verdicts: dict[str, str] = {}
    for e in rebid_entries:
        duid_label = None
        for line in e.get("message", "").splitlines():
            if not line.strip() or not line.startswith(" "):
                continue
            m = DUID_DROP_RE.match(line)
            if m:
                duid_label = m.group(1)
                continue
            m = DROP_REASON_RE.match(line)
            if m and duid_label:
                tag, _entrytype, explanation = m.groups()
                verdict = "TRIPPED" if tag == "FORCED" else "not a trip"
                duid_code = duid_label.split(" (")[0].strip()
                verdicts[duid_code] = f"{verdict}: {explanation}"
    return verdicts


def format_unit_events_overnight(customer_entries: list[dict], scada_entries: list[dict],
                                  rebid_entries: list[dict] | None = None) -> list[str]:
    """
    Merges customer_watcher + scada_drop_monitor into one per-unit section - what actually
    happened, not net/cumulative/worst statistics (you flagged those as giving a wrong
    picture: "if it fell and recovered it still fell"). Each unit gets one line listing every
    distinct move it made overnight, in order. scada_drop_monitor only ever logs falls (a
    subset of the same fuel types customer_watcher already tracks in both directions), so the
    same real event showing up in both sources is deduplicated by (DUID, prev, curr), not
    shown twice. This only changes how the recap summarises these two sources - the live
    per-interval ntfy pushes from customer_watcher.py/scada_drop_monitor.py are unchanged.

    If rebid_entries is given, any unit rebid_reconciler already has a trip verdict for gets
    it appended inline - you asked "surely that can be flagged as a trip" rather than needing
    to cross-check the separate "Drop reasons overnight" section. Only covers units whose drop
    falls on a calendar day rebid_reconciler has actually reconciled (yesterday, not today -
    see format_drop_reasons_overnight); a unit with no match here either hasn't been
    reconciled yet or its move was never big/relevant enough to reach rebid_reconciler at all.
    """
    if not customer_entries and not scada_entries:
        return []
    verdicts = build_trip_verdict_lookup(rebid_entries or [])
    all_entries = sorted(customer_entries + scada_entries, key=lambda x: x["ts"])
    events: dict[str, dict] = {}
    seen: set[tuple] = set()
    aggregate: dict[str, dict] = {}

    for e in all_entries:
        for line in e.get("message", "").splitlines():
            m = INDIVIDUAL_MOVE_RE.match(line)
            if m:
                label, region, fuel, prev_s, curr_s = m.groups()
                try:
                    prev_v = float(prev_s.replace(",", ""))
                    curr_v = float(curr_s.replace(",", ""))
                except ValueError:
                    continue
                label = label.strip()
                dedup_key = (label, round(prev_v, 1), round(curr_v, 1))
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                ts_str = datetime.fromisoformat(e["ts"]).strftime("%H:%M")
                d = events.setdefault(label, {"region": region.strip(), "fuel": (fuel or "?").strip(), "moves": []})
                d["moves"].append((ts_str, prev_v, curr_v))
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

    def fmt_signed(v: float) -> str:
        if v == 0:
            v = 0.0  # kills float's negative-zero sign bit - confirmed live, produced "+-0MW"
        return f"{'+' if v >= 0 else ''}{v:.0f}"

    lines = ["\nUnit activity overnight (customer_watcher + scada_drop_monitor, merged):"]
    if not events and not aggregate:
        lines.append("  None.")
        return lines

    for label in sorted(events, key=lambda k: events[k]["moves"][0][0]):
        d = events[label]
        moves = sorted(d["moves"], key=lambda t: t[0])
        bracket = f"{d['region']}, {d['fuel']}" if d["fuel"] != "?" else d["region"]
        move_strs = [f"{t} {p:.0f}->{c:.0f}MW" for t, p, c in moves]
        # events is keyed by the full label (may include station name, e.g. "TUMUT3
        # (Tumut3)") but verdicts is keyed by the bare DUID code - match on that instead.
        duid_code = label.split(" (")[0].strip()
        verdict_note = f" -- {verdicts[duid_code]}" if duid_code in verdicts else ""
        lines.append(f"  {label} [{bracket}]: " + ", ".join(move_strs) + verdict_note)

    if aggregate:
        for fuel in sorted(aggregate):
            d = aggregate[fuel]
            lines.append(f"  {fuel}: {d['count']} interval(s), net {fmt_signed(d['net_sum'])}MW")

    return lines


INTERCONNECTOR_CONSTRAINT_RE = re.compile(
    r"^\s*(.+?):\s*(-?\d+)MW,\s*(?:[A-Z0-9]+\s*->\s*[A-Z0-9]+,\s*)?(\d+)% of limit at (\d{2}:\d{2}) NEM time"
)


def format_pooled_interconnector_section(entries: list[dict]) -> list[str]:
    """
    Pools interconnector_monitor's own 'at/near limit' constraint lines across the overnight
    window by interconnector - each new 5-min crossing is logged as its own push with no
    clear/hysteresis pairing available here, so a sustained bind can repeat across many blocks.
    One summary line per interconnector instead: how many times flagged, the util% range seen.
    The "Planned outage(s) affecting..." portion of these same pushes is dropped entirely -
    format_network_outage_changes_overnight already covers ALL transmission assets (a strict
    superset of the 6 interconnectors this script tracks), so repeating it here is just
    duplicate noise (confirmed live: a 29-line dump for exactly this reason).
    """
    if not entries:
        return []
    stats: dict[str, dict] = {}
    for e in entries:
        for line in body_lines(e):
            m = INTERCONNECTOR_CONSTRAINT_RE.match(line)
            if not m:
                continue
            label = m.group(1).strip()
            util = int(m.group(3))
            d = stats.setdefault(label, {"count": 0, "min_util": util, "max_util": util})
            d["count"] += 1
            d["min_util"] = min(d["min_util"], util)
            d["max_util"] = max(d["max_util"], util)

    lines = [f"\ninterconnector_monitor ({len(entries)} interval(s) overnight):"]
    if not stats:
        lines.append("  None.")
        return lines
    for label in sorted(stats, key=lambda k: -stats[k]["count"]):
        d = stats[label]
        util_str = f"{d['min_util']}%" if d["min_util"] == d["max_util"] else f"{d['min_util']}-{d['max_util']}%"
        lines.append(f"  {label}: flagged {d['count']}x, {util_str} of limit")
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
    changes = []  # (kind, asset, region, start, finish)
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
            changes.append(("NEW", asset, region, row.get("Start"), row.get("Finish", "?")))
        elif prev_status != status:
            changes.append(("CHANGED", asset, region, row.get("Start"), row.get("Finish", "?")))

    nw.write_state(NETWORK_OUTAGE_RECAP_STATE_FILE, current)

    lines = ["\nNetwork outage changes overnight (all transmission assets):"]
    if first_run:
        lines.append("  (first run - establishing baseline, nothing to compare against yet)")
    elif not changes:
        lines.append("  None.")
    else:
        # Pool by asset - the same asset can announce many windows at once (recurring
        # maintenance), which would otherwise be one line per window. One summary line per
        # asset instead: how many NEW/CHANGED, spanning what date range.
        by_asset: dict[str, list] = {}
        for c in changes:
            by_asset.setdefault(c[1], []).append(c)
        for asset in sorted(by_asset, key=lambda a: -len(by_asset[a])):
            rows = by_asset[asset]
            region = rows[0][2]
            new_count = sum(1 for r in rows if r[0] == "NEW")
            changed_count = sum(1 for r in rows if r[0] == "CHANGED")
            kind_bits = []
            if new_count:
                kind_bits.append(f"{new_count} new")
            if changed_count:
                kind_bits.append(f"{changed_count} changed")
            # Parse as real dates before sorting - DD/MM/YYYY sorts wrong as a plain string
            # (confirmed live: "15/06/2027" sorted before "27/10/2026" lexicographically,
            # showing a span with the start AFTER the finish).
            def parse_hio_date(s: str) -> datetime | None:
                try:
                    return datetime.strptime(s, "%d/%m/%Y %H:%M")
                except (ValueError, TypeError):
                    return None
            starts = sorted((d for d in (parse_hio_date(r[3]) for r in rows) if d))
            finishes = sorted((d for d in (parse_hio_date(r[4]) for r in rows if r[4] != "?") if d))
            span = f", {starts[0].strftime('%d/%m/%Y %H:%M')} to {finishes[-1].strftime('%d/%m/%Y %H:%M')}" if starts and finishes else ""
            lines.append(f"  {asset} [{region}]: {', '.join(kind_bits)}{span}")
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

    # Two separate sections (not one nested under the other) - fetched once here since the
    # actuals download is the expensive part (~2min), shared between both.
    lines = ["\nAverage spot price overnight:"]
    try:
        actuals = fetch_actual_prices_for_ranges([{"start": since_naive, "end": now_naive}])
    except Exception as exc:
        lines.append(f"  Could not fetch actuals ({exc}).")
        lines.append("\nCap payouts overnight:")
        lines.append(f"  Could not fetch actuals ({exc}).")
        return lines

    avg_lines = []
    payouts = []
    for region in regions:
        prices = actuals.loc[actuals["REGIONID"] == region, "RRP"] if not actuals.empty else []
        if len(prices) == 0:
            continue
        avg_lines.append(f"  {region}: ${prices.mean():,.0f}/MWh")
        _, payout_full = nw.cap_settlement(prices, strike, interval_hours=5 / 60)
        payout = payout_full / 24
        if payout > 0:
            payouts.append(f"  {region}: ${payout:,.2f}")

    lines.extend(avg_lines if avg_lines else ["  No data available."])

    lines.append("\nCap payouts overnight:")
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


def _coal_fleet_live_full() -> dict:
    """
    Read-only live coal-fleet check, reusing coal_fleet_trend.py's own logic - fetches fresh
    MTPASA data but does NOT append to coal_fleet_history.csv (that script's own daily run
    already does; a recap-triggered append would just duplicate/pollute the trend history).
    Returns the full picture (current MW/capacity/label plus week/month/year-ago deltas), not
    just the month-ago figure - shared by the DAY AHEAD coal section and the QED commentary's
    threshold check so both come from a single fetch instead of two.
    """
    files = nw.get_latest_files(cft.PASA_URL, cft.PASA_PATTERN, n=1)
    df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(files[-1])), "MTPASA_DUIDAVAILABILITY")
    df["PASAAVAILABILITY"] = pd.to_numeric(df["PASAAVAILABILITY"], errors="coerce")
    df["_day_dt"] = df["DAY"].apply(lambda s: datetime.strptime(s.strip(), "%Y/%m/%d %H:%M:%S"))

    registry = nw.load_registry()
    fuel_filtered = False
    total_capacity_mw = None
    if registry.fuel_info is not None:
        fcol = cft.fuel_column(registry.fuel_info)
        if fcol:
            coal_rows = registry.fuel_info[registry.fuel_info[fcol].str.lower().isin(cft.COAL_FUELS)]
            coal_duids = set(coal_rows["DUID"])
            df = df[df["DUID"].isin(coal_duids)]
            fuel_filtered = True
            if "CAPACITY" in coal_rows.columns:
                total_capacity_mw = pd.to_numeric(coal_rows["CAPACITY"], errors="coerce").sum()

    now_naive = datetime.now(cft.NEM_TZ).replace(tzinfo=None)
    window_end = now_naive + timedelta(days=cft.FORWARD_WINDOW_DAYS)
    window = df[(df["_day_dt"] >= now_naive) & (df["_day_dt"] <= window_end)]
    if window.empty:
        return {"avg_available_mw": None}
    avg_available_mw = window.groupby(window["_day_dt"].dt.date)["PASAAVAILABILITY"].sum().mean()

    history = cft.read_history()
    deltas = {}
    for period_name, days_ago in (("week-ago", 7), ("month-ago", 30), ("year-ago", 365)):
        ref = cft.closest_entry(history, now_naive - timedelta(days=days_ago), fuel_filtered)
        if ref is None:
            deltas[period_name] = None
            continue
        ref_val = float(ref["avg_available_mw"])
        delta = avg_available_mw - ref_val
        pct = (delta / ref_val * 100) if ref_val else None
        deltas[period_name] = {"date": ref["date"], "delta_mw": delta, "pct": pct}

    return {
        "avg_available_mw": avg_available_mw,
        "total_capacity_mw": total_capacity_mw,
        "fuel_filtered": fuel_filtered,
        "label": "Coal" if fuel_filtered else "All-fleet (unfiltered)",
        "deltas": deltas,
    }


def format_coal_fleet_section(latest: dict | None) -> list[str]:
    """
    Straight log-replay of coal_fleet_trend's own push - you asked for it exactly as it
    appears in the individual ntfy push, same treatment as gas_spread's section. Shows the
    full message, not body_lines() (which drops the first line) - coal_fleet_trend's first
    line IS the headline figure, not a throwaway category label like gas_spread's "Domestic
    east-coast gas hub prices ($/GJ):" line.
    """
    if latest is None:
        return ["\ncoal_fleet_trend: no data available."]
    lines = ["\ncoal_fleet_trend:"]
    lines.extend(f"  {ln.strip()}" for ln in latest.get("message", "").splitlines() if ln.strip())
    return lines


def _negative_pricing_live() -> dict[str, float]:
    """Read-only live negative-pricing check, reusing negative_pricing_tracker.py's own logic -
    fetches the trailing week fresh but does NOT append to negative_pricing_history.csv (same
    reasoning as _coal_fleet_live_full). Stays QED-commentary-only per your call - not its own
    DAY AHEAD section."""
    cfg = nw.CONFIG
    regions = cfg.get("nem_regions", ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"])
    now_naive = datetime.now(npt.NEM_TZ).replace(tzinfo=None)

    all_rows = []
    for days_ago in range(1, npt.DAYS_TO_AGGREGATE + 1):
        day = now_naive - timedelta(days=days_ago)
        compact = day.strftime("%Y%m%d")
        try:
            files = nw.list_nemweb_files(npt.PUBLIC_PRICES_URL, rf"^PUBLIC_PRICES_{compact}0000_\d+\.zip$")
        except Exception:
            continue
        if not files:
            continue
        try:
            df = nw.get_table(nw.parse_mms_zip(nw.download_bytes(files[-1])), "DREGION")
        except Exception:
            continue
        df["RRP"] = pd.to_numeric(df["RRP"], errors="coerce")
        df = df.drop_duplicates(subset=["SETTLEMENTDATE", "REGIONID"])
        all_rows.append(df)

    if not all_rows:
        return {}
    combined = pd.concat(all_rows, ignore_index=True)
    result: dict[str, float] = {}
    for region in regions:
        region_df = combined[combined["REGIONID"] == region]
        if region_df.empty:
            continue
        result[region] = (region_df["RRP"] <= 0).mean() * 100
    return result


PEAK_DEMAND_MONTHS = {12, 1, 2, 6, 7, 8}  # Australian summer (cooling) + winter (heating) demand peaks


def _overlaps_peak_season(start_date, end_date) -> bool:
    d = start_date.replace(day=1)
    while d <= end_date:
        if d.month in PEAK_DEMAND_MONTHS:
            return True
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return False


def format_qed_commentary(now: datetime, by_source: dict[str, list[dict]]) -> list[str]:
    """
    QED-grounded pattern commentary - revives causal_rules.py's logic, orphaned since
    market_read.py (its only caller) was deleted this session; only its gas-spread rule had
    been manually ported into this recap so far (format_gas_section's watch level). Same terse
    citation style as that line, not causal_rules.py's original verbose Finding/precedent
    paragraphs. One combined block at the end of the recap, not threaded through every section.
    Reuses data already gathered elsewhere in this run wherever possible (drops, spikes,
    interconnector flags, gas spread, PASA cache); coal_fleet_trend and negative_pricing_tracker
    get their own live read-only check since both are change-gated at the source (same
    staleness risk that drove cap/predispatch/reserve to go live earlier).
    """
    patterns: list[str] = []

    # 1. Drop coincides with an active price spike in the same region - "the single most
    # repeated mechanism in the QED dataset" (Callide 2021, the 2022 crisis, Nov 2024 NSW/QLD).
    drop_regions: set[str] = set()
    for e in by_source.get("customer_watcher", []) + by_source.get("scada_drop_monitor", []):
        for line in e.get("message", "").splitlines():
            m = INDIVIDUAL_MOVE_RE.match(line)
            if m:
                drop_regions.add(m.group(2).strip())
    spike_regions = {region_key(line) for _, line in net_effect_lines(by_source.get("spot_spike", []))}
    spike_regions.discard(None)
    coincide = drop_regions & spike_regions
    if coincide:
        patterns.append(
            f"Drop + active price spike in the same region ({', '.join(sorted(coincide))}) - the single "
            f"most repeated mechanism in the QED dataset (Callide 2021, the 2022 crisis, Nov 2024 NSW/QLD)."
        )

    # 2. Heywood/Murraylink bound + SA price elevated - the most repeated SA divergence pattern.
    ic_flagged: set[str] = set()
    for e in by_source.get("interconnector_monitor", []):
        for line in e.get("message", "").splitlines():
            m = INTERCONNECTOR_CONSTRAINT_RE.match(line)
            if m:
                ic_flagged.add(m.group(1).strip())
    sa_bound = any("Heywood" in n or "Murraylink" in n for n in ic_flagged)
    if sa_bound and "SA1" in spike_regions:
        patterns.append(
            "Heywood/Murraylink at/near limit while SA1 has an active price spike - the most repeated "
            "cause of SA price divergence in the QED history (recurred 2023, 2024, 2025, Jan 2026)."
        )

    # 3. Gas spread - reuse the same state file the dedicated gas section already reads.
    gas_state = nw.read_state("gas_spread_state.json", default={})
    gas_spread = gas_state.get("spread_aud_gj")
    if gas_spread is not None and abs(gas_spread) >= GAS_SPREAD_WATCH_THRESHOLD:
        patterns.append(
            f"Gas spread ${gas_spread:+.2f}/GJ - the leading indicator for domestic gas (and VIC/SA spot "
            f"price floor) repricing; preceded the 2018 tightening, 2021 Callide-quarter spike, and the "
            f"2022 crisis by weeks to months."
        )

    # 4. Coal fleet decline - live (own script is change-gated, log-replay could be stale).
    try:
        coal_full = _coal_fleet_live_full()
        month_ago = coal_full.get("deltas", {}).get("month-ago")
        coal_pct = month_ago["pct"] if month_ago else None
    except Exception as exc:
        coal_pct = None
        print(f"[notification_recap] WARNING: coal fleet live check failed: {exc}")
    if coal_pct is not None and coal_pct <= -5:
        patterns.append(
            f"Coal fleet availability down {abs(coal_pct):.1f}% vs a month ago - QED history shows "
            f"structural coal decline (not single outages) has been the dominant multi-quarter price "
            f"driver since 2023."
        )

    # 5. Negative pricing trend - live (same reason as coal fleet).
    try:
        neg_regions = _negative_pricing_live()
    except Exception as exc:
        neg_regions = {}
        print(f"[notification_recap] WARNING: negative pricing live check failed: {exc}")
    high_neg = {r: p for r, p in neg_regions.items() if p >= 15}
    if high_neg:
        bits = ", ".join(f"{r} {p:.1f}%" for r, p in sorted(high_neg.items()))
        patterns.append(
            f"Negative/zero pricing >=15% of intervals this week ({bits}) - QED history shows this "
            f"climbing almost monotonically (3.6% Q2 2020 -> 31.0% Q4 2025) as solar growth outpaces "
            f"midday demand; expected structural trend, not a fault, unless paired with curtailment."
        )

    # 6. Upcoming outages in peak-demand season - individual outages rarely move price alone,
    # but coinciding with a demand peak (summer/winter) does.
    cache = nw.read_state("pasa_recent_windows.json", default=[])
    today = now.date()
    peak_count = 0
    for w in cache:
        if w.get("delta", 0) >= 0:
            continue
        try:
            start_d = datetime.strptime(w["start"], "%Y-%m-%d").date()
            end_d = datetime.strptime(w["end"], "%Y-%m-%d").date()
        except (ValueError, KeyError, TypeError):
            continue
        if end_d < today:
            continue
        if _overlaps_peak_season(start_d, end_d):
            peak_count += 1
    if peak_count:
        patterns.append(
            f"{peak_count} declared outage window(s) fall within peak-demand season (Dec-Feb/Jun-Aug) - "
            f"QED history shows outages rarely move price alone, but coinciding with a demand peak does."
        )

    lines = ["\n=== QED COMMENTARY ==="]
    if not patterns:
        lines.append("\nNo QED-flagged patterns matched overnight/today.")
        return lines

    if len(patterns) >= 2:
        lines.append(
            f"\n{len(patterns)} independent QED-validated precursor patterns active at once - real "
            f"documented crises (2022 in particular: gas + coal + cold snap together) rarely came from "
            f"one signal alone."
        )
    else:
        lines.append("")
    for p in patterns:
        lines.append(f"  - {p}")
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
    # Cap payouts and predispatch forecast-vs-actual lead the section - always print something
    # (a payout/eventuate check or "none"), so they're the first thing you see regardless of
    # whether anything else fired overnight.
    lines.extend(format_cap_overnight_section(now, since))
    lines.extend(format_predispatch_eventuated_section(now, log_entries))
    lines.extend(format_drop_reasons_overnight(by_source.get("rebid_reconciler", [])))

    overnight_had_content = False
    for source in ASIS_DISCRETE_SOURCES:
        if source in by_source:
            overnight_had_content = True
            lines.extend(format_discrete_section(source, by_source[source]))
    if "customer_watcher" in by_source or "scada_drop_monitor" in by_source:
        overnight_had_content = True
        lines.extend(format_unit_events_overnight(
            by_source.get("customer_watcher", []), by_source.get("scada_drop_monitor", []),
            by_source.get("rebid_reconciler", [])
        ))
    if "interconnector_monitor" in by_source:
        overnight_had_content = True
        lines.extend(format_pooled_interconnector_section(by_source["interconnector_monitor"]))
    for source in THRESHOLD_SOURCES:
        section = format_threshold_section(source, by_source.get(source, []))
        if section:
            overnight_had_content = True
            lines.extend(section)
    if not overnight_had_content:
        lines.append("\nQuiet - no other alerts overnight.")
    lines.extend(format_network_outage_changes_overnight(now))

    lines.append("\n\n=== DAY AHEAD - what's coming ===")
    lines.extend(format_cap_section(now))
    lines.extend(format_predispatch_forecast_section(now))

    latest_coal = None
    for e in log_entries:
        if e["source"] == "coal_fleet_trend" and (latest_coal is None or e["ts"] > latest_coal["ts"]):
            latest_coal = e
    lines.extend(format_coal_fleet_section(latest_coal))

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

    lines.extend(format_qed_commentary(now, by_source))

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
