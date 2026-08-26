"""
Rebid Monitor - Stage 2: Next-morning reason reconciler

Reads yesterday's MW-drop events (logged by scada_drop_monitor.py into
state/scada_drops.csv) and matches each triggered DUID against its
BIDDAYOFFER_D rebid explanation text for that day, grouped by company.

Run once daily, ~5am NEM time (after Bidmove_Complete publishes, ~4am).

Tagging: BIDTYPE is filtered to ENERGY (the FCAS bid types have their own
noisy rebid chatter that isn't relevant to an MW-output drop). Each
explanation is loosely tagged FORCED / ECONOMIC / OTHER by keyword match -
per the QED historical remap, AEMO's own reporting increasingly
distinguishes genuine forced outages from generators economically
withdrawing capacity (a distinct and growing behaviour in the ageing coal
fleet), and that distinction is useful context a plain "here's the rebid
text" dump doesn't give you. It's a heuristic, not authoritative - always
read the actual REBIDEXPLANATION text, the tag is just a sorting aid.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import nemweb_common as nw

BIDMOVE_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Bidmove_Complete/"

NEM_TZ = timezone(timedelta(hours=10))
DROPS_LOG = nw.STATE_DIR / "scada_drops.csv"

FORCED_KEYWORDS = ["forced", "trip", "fault", "failure", "unplanned", "breaker",
                   "boiler", "loss of", "tube leak", "emergency", "protection"]
ECONOMIC_KEYWORDS = ["price", "market", "economic", "high price", "withdraw",
                     "demand", "expectation", "commercial"]


def classify_explanation(text: str) -> str:
    lower = (text or "").lower()
    if any(k in lower for k in FORCED_KEYWORDS):
        return "FORCED"
    if any(k in lower for k in ECONOMIC_KEYWORDS):
        return "ECONOMIC"
    return "OTHER"


def read_drops_for_date(target_date: str) -> list[dict]:
    if not DROPS_LOG.exists():
        return []
    with open(DROPS_LOG, newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r["date"] == target_date]


def rewrite_drops_log_excluding(target_date: str) -> None:
    """Clear yesterday's entries once reconciled, keep everything else."""
    if not DROPS_LOG.exists():
        return
    with open(DROPS_LOG, newline="") as f:
        rows = list(csv.DictReader(f))
    remaining = [r for r in rows if r["date"] != target_date]
    with open(DROPS_LOG, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "time", "duid", "region", "station",
                                                "previous_mw", "current_mw", "drop_mw"])
        writer.writeheader()
        writer.writerows(remaining)


def main() -> None:
    cfg = nw.CONFIG
    topic = cfg.get("ntfy_topics", {}).get("rebid", "rebid-alerts")

    yesterday = (datetime.now(NEM_TZ).replace(tzinfo=None) - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_compact = yesterday.replace("-", "")

    drops = read_drops_for_date(yesterday)
    if not drops:
        print(f"[rebid_reconciler] No SCADA drops logged for {yesterday} - nothing to reconcile.")
        return

    triggered_duids = {d["duid"] for d in drops}

    try:
        files = nw.list_nemweb_files(BIDMOVE_URL, rf"^PUBLIC_BIDMOVE_COMPLETE_{yesterday_compact}_\d+\.zip$")
    except Exception as exc:
        print(f"[rebid_reconciler] ERROR listing Bidmove_Complete: {exc}")
        return

    if not files:
        print(f"[rebid_reconciler] No Bidmove_Complete file published yet for {yesterday} - try again later.")
        return

    table = nw.download_and_get_table(files[-1], "BIDDAYOFFER_D")
    table = table[(table["BIDTYPE"] == "ENERGY") & (table["DUID"].isin(triggered_duids))]

    registry = nw.load_registry()
    reg_lookup = registry.merged.set_index("DUID") if registry.merged is not None else None

    # Group by owner (from the registry, same readable company name customer_watcher.py and
    # scada_drop_monitor.py now use) rather than AEMO's raw PARTICIPANTID code - falls back to
    # that code only if a DUID isn't in the registry at all.
    by_owner: dict[str, dict[str, list[dict]]] = {}
    for _, row in table.sort_values(["DUID", "LASTCHANGED"]).iterrows():
        duid = row["DUID"]
        owner = None
        if reg_lookup is not None and duid in reg_lookup.index:
            owner = reg_lookup.loc[duid].get("Owner")
        owner = owner or row.get("PARTICIPANTID") or "(unknown participant)"
        by_owner.setdefault(owner, {}).setdefault(duid, []).append({
            "entrytype": row["ENTRYTYPE"],
            "explanation": row["REBIDEXPLANATION"],
            "lastchanged": row["LASTCHANGED"],
            "tag": classify_explanation(row["REBIDEXPLANATION"]),
        })

    drops_by_duid: dict[str, list[dict]] = {}
    for d in drops:
        drops_by_duid.setdefault(d["duid"], []).append(d)

    lines = [f"Rebid reconciliation for {yesterday} - {len(triggered_duids)} DUID(s) with MW drops:"]
    matched_duids = set()

    for owner, duids in by_owner.items():
        lines.append(f"\n{owner}:")
        for duid, entries in duids.items():
            matched_duids.add(duid)
            station = ""
            if reg_lookup is not None and duid in reg_lookup.index:
                station = reg_lookup.loc[duid].get("STATIONNAME") or reg_lookup.loc[duid].get("UNIT_NAME") or ""
            label = duid + (f" ({station})" if station else "")
            drop_summary = ", ".join(f"{d['time']} ({d['drop_mw']}MW)" for d in drops_by_duid.get(duid, []))
            lines.append(f"  {label} - drop(s) at {drop_summary}")
            seen_texts = set()
            for e in entries:
                if e["explanation"] in seen_texts:
                    continue
                seen_texts.add(e["explanation"])
                lines.append(f"    [{e['tag']}/{e['entrytype']}] {e['explanation']}")

    unmatched = triggered_duids - matched_duids
    if unmatched:
        lines.append(f"\nNo ENERGY rebid/offer text found for: {', '.join(sorted(unmatched))}")

    message = "\n".join(lines)
    print(message)

    nw.push_ntfy(
        topic=topic,
        title=f"Rebid reconciliation - {yesterday}",
        message=message,
        tags=["memo"],
    )

    rewrite_drops_log_excluding(yesterday)


if __name__ == "__main__":
    main()
