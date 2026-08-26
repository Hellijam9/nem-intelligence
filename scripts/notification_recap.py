"""
Script: Notification Recap (morning / weekend rollup)

This does NOT pull any live NEMWEB data itself. It reads back the shared
notification log (every push_ntfy() call across every script in this system
now appends to state/notification_log.jsonl) and reports what actually fired
since the last time this recap ran - "what happened overnight", per your
request, rather than another live snapshot.

Runs once a day at 7:00am on weekdays. Because it only runs Monday-Friday,
Monday's run naturally spans since Friday's recap (covering Friday evening
through the whole weekend) with no separate weekend logic needed beyond the
title/label - it's still just "everything logged since the last successful
recap".

Deliberately excludes the market_read and recap's own topics from the
rollup: those are the 24h-ahead forward-looking views, not live alerts, and
would just be noise repeated here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import nemweb_common as nw
from nemweb_common import NEM_TZ

STATE_FILE = "recap_state.json"
EXCLUDED_TOPIC_KEYS = ("market_read", "recap")
MAX_LINES_PER_ENTRY = 5


def format_entry(entry: dict) -> list[str]:
    ts = datetime.fromisoformat(entry["ts"])
    time_str = ts.strftime("%a %H:%M")
    title = entry.get("title") or entry.get("source", "")
    lines = [f"  [{time_str}] {title}"]
    body_lines = [ln for ln in entry.get("message", "").splitlines() if ln.strip()]
    shown = body_lines[:MAX_LINES_PER_ENTRY]
    lines.extend(f"    {ln}" for ln in shown)
    if len(body_lines) > MAX_LINES_PER_ENTRY:
        lines.append(f"    ...and {len(body_lines) - MAX_LINES_PER_ENTRY} more line(s)")
    return lines


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("recap", "nem-recap")
    excluded_topics = {cfg.get("ntfy_topics", {}).get(key) for key in EXCLUDED_TOPIC_KEYS}

    now = datetime.now(NEM_TZ)
    state = nw.read_state(STATE_FILE, default={})
    last_recap_at = state.get("last_recap_at")
    if last_recap_at:
        since = datetime.fromisoformat(last_recap_at)
    else:
        since = now - timedelta(hours=24)

    is_monday = now.weekday() == 0
    label = "Weekend Recap" if is_monday else "Morning Recap"
    span_desc = "since Friday" if is_monday else "overnight"

    entries = [
        e for e in nw.read_notification_log(since=since)
        if e.get("topic") not in excluded_topics
    ]

    lines = [f"{label}: {span_desc} ({since.strftime('%a %d-%b %H:%M')} to {now.strftime('%a %d-%b %H:%M')})"]

    if not entries:
        lines.append("\nQuiet - no alerts fired in this window.")
    else:
        by_source: dict[str, list[dict]] = {}
        for e in entries:
            by_source.setdefault(e.get("source", "unknown"), []).append(e)
        lines.append(f"\n{len(entries)} alert(s) across {len(by_source)} monitor(s):")
        for source in sorted(by_source):
            lines.append(f"\n{source}:")
            for e in by_source[source]:
                lines.extend(format_entry(e))

    message = "\n".join(lines)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title=label,
        message=message,
        tags=["sunrise"] if not is_monday else ["sunrise", "calendar"],
    )

    nw.write_state(STATE_FILE, {"last_recap_at": now.isoformat()})


if __name__ == "__main__":
    main()
