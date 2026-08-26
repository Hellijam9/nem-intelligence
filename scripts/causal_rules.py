"""
Causal rule library - the QED historical mechanics (see the "What Actually
Moves AEMO Wholesale Electricity Prices" report and nem_intelligence_system_remap.md)
encoded as pattern-matching rules over live signals from the running scripts.

Each rule takes the assembled `signals` dict (see market_read.py) and yields
zero or more Finding objects. This is deliberately simple pattern-matching,
not a model - it flags "this live condition matches a pattern that recurred
in 8 years of QED history" and cites the precedent, so a human still makes
the final call. Nothing here predicts anything; it interprets what's
already happened/is happening against known historical patterns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Finding:
    headline: str
    detail: str
    precedent: str  # the QED historical pattern this matches
    severity: str = "info"  # info / watch / notable
    # Which region(s) this finding is genuinely about, from the actual underlying data each
    # rule processed - NOT derived by scanning the rendered headline/detail text later (that
    # broke once findings started capping long lists for display, e.g. the PASA one showing
    # only its 8 largest windows - the other 124 could easily involve regions never mentioned
    # in the visible text). Empty set = genuinely NEM-wide, not "unknown."
    regions: frozenset[str] = field(default_factory=frozenset)
    # Per-region detail text, when a finding's headline count (e.g. "133 outage windows")
    # would otherwise get repeated identically against every region in `regions` - misleading,
    # since one region might have 2 of the 133 and another 40. Falls back to the plain headline
    # if a rule doesn't populate this (e.g. single-region findings where the headline is already
    # region-specific and unambiguous).
    region_detail: dict[str, str] = field(default_factory=dict)


PRIORITY_INTERCONNECTORS = {"V-SA": "Heywood", "V-S-MNSP1": "Murraylink"}


def rule_extreme_spot_price(signals: dict) -> list[Finding]:
    findings = []
    thresholds = signals.get("spot_spike_thresholds", [300, 1000, 5000, 15000])
    drops_today = signals.get("scada_drops_today", [])
    for region, price in signals.get("current_prices", {}).items():
        if price is None:
            continue
        tier = sum(1 for t in thresholds if price >= t)
        if tier == 0:
            continue

        # Actually check for a matching SCADA drop rather than telling the reader to go
        # look at the raw-data section themselves - the data's right here in `signals`.
        region_drops = [d for d in drops_today if d.get("region") == region]
        if region_drops:
            drop_bits = "; ".join(
                f"{d['duid']} ({d.get('station') or 'unknown'}) {d['drop_mw']:+.0f}MW at {d['time']}"
                for d in region_drops
            )
            corroboration = f"Matching SCADA drop(s) today in {region}: {drop_bits}."
        else:
            corroboration = (
                f"No matching SCADA drop logged today in {region} - check the predispatch "
                f"forecast, or this may be demand-driven rather than outage-driven."
            )

        findings.append(Finding(
            headline=f"{region} spot price is ${price:,.0f}/MWh right now",
            detail=f"Crossed the ${thresholds[tier-1]:,.0f} threshold. {corroboration}",
            precedent="Every genuinely extreme spot event in 8 years of QED history traces to either "
                      "an extreme demand event (heatwave/cold snap) or a coincident coal/gas outage - "
                      "never to insufficient renewables.",
            severity="notable" if tier >= 2 else "watch",
            regions=frozenset({region}),
        ))
    return findings


def rule_outage_coincides_with_price_move(signals: dict) -> list[Finding]:
    findings = []
    drops_today = signals.get("scada_drops_today", [])
    active_tiers = signals.get("active_spot_tiers", {})
    for drop in drops_today:
        region = drop.get("region")
        if region and active_tiers.get(region, -1) >= 0:
            findings.append(Finding(
                headline=f"{drop['duid']} ({drop.get('station') or 'unknown station'}) dropped "
                         f"{abs(drop['drop_mw']):.0f}MW in {region} today, and {region} has an active price-tier breach",
                detail=f"Drop logged at {drop['time']}: {drop['previous_mw']:.0f} -> {drop['current_mw']:.0f}MW.",
                precedent="This is the single most repeated mechanism in the QED dataset - Callide (2021), "
                          "the 2022 crisis, and the Nov 2024 NSW/QLD events were all single or clustered coal/gas "
                          "outages coinciding with a price spike, not demand growth.",
                severity="notable",
                regions=frozenset({region}),
            ))
    return findings


def rule_sa_interconnector_constraint(signals: dict) -> list[Finding]:
    findings = []
    flagged = signals.get("interconnectors_flagged", {})
    sa_active = signals.get("active_spot_tiers", {}).get("SA1", -1) >= 0
    sa_price = signals.get("current_prices", {}).get("SA1")
    bound = [name for ic_id, name in PRIORITY_INTERCONNECTORS.items() if flagged.get(ic_id)]
    if bound and sa_active:
        findings.append(Finding(
            headline=f"{' and '.join(bound)} at/near limit while SA pricing is elevated",
            detail=f"SA1 spot price: ${sa_price:,.2f}/MWh." if sa_price is not None else "SA1 spot price unavailable.",
            precedent="The single most repeated cause of SA price divergence in the QED history - this exact "
                      "pattern (Heywood/Murraylink constrained + SA can't import cheaper power) recurred in "
                      "Q2/Q3 2023, Q3 2024, Q3 2025, and the Jan 2026 heat event. It's structural, not a one-off, "
                      "and will keep recurring on hot/still days until further SA interconnection is built.",
            severity="notable",
            regions=frozenset({"SA1", "VIC1"}),  # Heywood/Murraylink both connect SA-VIC specifically
        ))
    return findings


def rule_negative_pricing_trend(signals: dict) -> list[Finding]:
    findings = []
    for region, info in signals.get("negative_pricing", {}).items():
        pct = info.get("pct")
        if pct is None or pct < 15:
            continue
        trend_bits = info.get("trend", "")
        findings.append(Finding(
            headline=f"{region} negative/zero pricing at {pct:.1f}% of intervals (trailing week)",
            detail=trend_bits or "No comparison history yet.",
            precedent="Negative-price frequency climbed almost monotonically across the QED dataset - "
                      "3.6% (Q2 2020) to an all-time 31.0% (Q4 2025) - driven by rooftop/grid solar growth "
                      "outpacing midday demand. This is the expected structural trend, not a fault condition, "
                      "unless it's paired with curtailment or system-strength directions.",
            severity="info",
            regions=frozenset({region}),
        ))
    return findings


def rule_coal_fleet_decline(signals: dict) -> list[Finding]:
    coal = signals.get("coal_fleet_trend", {})
    pct_change = coal.get("month_ago_pct")
    if pct_change is None or pct_change > -5:
        return []
    return [Finding(
        headline=f"Aggregate {'coal' if coal.get('fuel_filtered') else 'fleet'} availability down "
                 f"{abs(pct_change):.1f}% vs a month ago",
        detail=f"Current 7-day-forward average: {coal.get('current_mw', 0):,.0f}MW.",
        precedent="QED history shows structural coal-fleet decline (not single outages) has been the dominant "
                  "multi-quarter price driver since 2023 - coal fell below 50% of NEM supply for the first "
                  "time in Q4 2024. Distinct from the acute outage-driven spikes flagged elsewhere in this report.",
        severity="watch",
    )]


def rule_gas_spread(signals: dict) -> list[Finding]:
    gas = signals.get("gas_spread")
    if gas is None:
        return []
    spread = gas.get("spread_aud_gj")
    if spread is None:
        return []
    # Threshold checked against real history, not picked arbitrarily: your own QED dataset
    # shows domestic gas at $10.60/GJ in Q4 2021 while JKM was already spiking toward its
    # ~$60/MMBtu December 2021 record (~$25-40/GJ AUD equivalent) - a $25-40/GJ spread, TWO
    # QUARTERS before domestic gas actually caught up to "LNG parity" at $28.40/GJ in Q2 2022.
    # $15/GJ sits below that real lead-up range (so it'd catch the buildup phase, not just the
    # eventual peak) but well above ordinary day-to-day noise (a $4 threshold, the original
    # pick here, would have fired on completely unremarkable days).
    findings = []
    if abs(spread) >= 15.0:
        stale = " (benchmark may be stale - check config.json)" if gas.get("stale") else ""
        findings.append(Finding(
            headline=f"International LNG trading ${spread:+.2f}/GJ vs domestic gas{stale}",
            detail=f"Domestic avg ${gas.get('domestic_avg', 0):.2f}/GJ vs JKM-equivalent ${gas.get('jkm_aud_gj', 0):.2f}/GJ.",
            precedent="This exact spread is the leading indicator for domestic gas (and therefore the VIC/SA "
                      "spot price floor) repricing in the QED history - it preceded the 2018 tightening, the "
                      "2021 Callide-quarter gas spike, and the full 2022 crisis by weeks to months. A widening "
                      "spread that persists (not a one-day blip) is what to watch for.",
            severity="watch",
        ))
    return findings


PEAK_DEMAND_MONTHS = {12, 1, 2, 6, 7, 8}  # Australian summer (cooling) + winter (heating) demand peaks


def _overlaps_peak_season(start_str: str, end_str: str) -> bool:
    """Whether any month in [start, end] falls in the NEM's known peak-demand months."""
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    y, m = start.year, start.month
    for _ in range(60):  # bounded generously - no realistic PASA window spans 5 years
        if m in PEAK_DEMAND_MONTHS:
            return True
        if (y, m) >= (end.year, end.month):
            break
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return False


def rule_pasa_outage_upcoming(signals: dict) -> list[Finding]:
    # Actually performs the seasonal cross-check itself now, rather than telling the reader to
    # go do it - same fix pattern already applied to rule_extreme_spot_price (which used to say
    # "check below" and now checks signals["scada_drops_today"] directly). Grouped into at most
    # 2 findings total (not one per window) - the explanation only needs saying once, the
    # windows are just line items under it.
    windows = signals.get("pasa_upcoming_windows", [])
    if not windows:
        return []

    MAX_LINES = 8  # a normal 3-hourly diff is a handful of windows - this only bites after a
    # gap (e.g. tasks down for days), where one run's diff can otherwise dump dozens/hundreds
    # of lines into a single notification. Caps the visible list, doesn't drop the count.

    peak_reductions, other = [], []
    for w in windows:
        line = (f"{w['duid']} ({w.get('station') or 'unknown'}) [{w['region']}, {w.get('owner') or 'unknown'}]: "
                f"{w['delta']:+.0f}MW, {w['start']} to {w['end']}")
        entry = (abs(w["delta"]), line, w["region"])
        if w["delta"] < 0 and _overlaps_peak_season(w["start"], w["end"]):
            peak_reductions.append(entry)
        else:
            other.append(entry)

    def capped_lines(entries: list[tuple[float, str, str]]) -> list[str]:
        entries = sorted(entries, key=lambda e: e[0], reverse=True)
        lines = [line for _, line, _ in entries[:MAX_LINES]]
        if len(entries) > MAX_LINES:
            lines.append(f"...and {len(entries) - MAX_LINES} more (largest {MAX_LINES} shown above by MW size)")
        return lines

    def all_regions(entries: list[tuple[float, str, str]]) -> frozenset[str]:
        # From every entry, not just the capped/displayed ones - a region can be genuinely
        # implicated by an outage that didn't make the top-N cut for display.
        return frozenset(region for _, _, region in entries)

    def region_counts(entries: list[tuple[float, str, str]], noun: str) -> dict[str, str]:
        # Per-region count, not the finding's flat total repeated against every region -
        # otherwise NSW1 (which might have 40 of the 133 windows) reads identically to TAS1
        # (which might have 2), which is misleading about how much each region is really
        # implicated.
        counts: dict[str, int] = {}
        for _, _, region in entries:
            counts[region] = counts.get(region, 0) + 1
        total = len(entries)
        return {r: f"{c} of the {total} {noun} affect {r}" for r, c in counts.items()}

    findings = []
    if peak_reductions:
        findings.append(Finding(
            headline=f"{len(peak_reductions)} declared outage window(s) fall within peak-demand season (Dec-Feb / Jun-Aug)",
            detail="\n".join(capped_lines(peak_reductions)),
            precedent="QED history shows individual planned outages rarely move price on their own - it's "
                      "specifically outages coinciding with high-demand periods (summer/winter peaks) that "
                      "spike prices. These fall within that window, so they're worth watching, not just noting.",
            severity="watch",
            regions=all_regions(peak_reductions),
            region_detail=region_counts(peak_reductions, "peak-season outage window(s)"),
        ))
    if other:
        findings.append(Finding(
            headline=f"{len(other)} other declared availability change(s) in the next 30 days",
            detail="\n".join(capped_lines(other)),
            precedent="Outside a peak-demand period, or capacity returning to service rather than a reduction - "
                      "lower standalone risk per the QED history, included here for visibility.",
            severity="info",
            regions=all_regions(other),
            region_detail=region_counts(other, "other availability change(s)"),
        ))
    return findings


ALL_RULES = [
    rule_extreme_spot_price,
    rule_outage_coincides_with_price_move,
    rule_sa_interconnector_constraint,
    rule_negative_pricing_trend,
    rule_coal_fleet_decline,
    rule_gas_spread,
    rule_pasa_outage_upcoming,
]


def rule_compound_risk(signals: dict, other_findings: list[Finding]) -> list[Finding]:
    """
    Looks ACROSS what the other 7 rules already found (not raw signals directly) - if two or
    more independent QED-validated precursor patterns are active at once, produces one
    consolidated "here's what could happen" synthesis. Grounded in the QED history itself:
    real documented crises (2022 in particular - gas price shock + coal outages + a cold snap,
    all at once) came from multiple things converging, not any single signal alone. Also pulls
    in live demand + wind/solar output (the actual "hot+calm" ingredients behind the SA/Heywood
    pattern) and the forward weather forecast for whichever region(s) are already implicated,
    as supporting context for the synthesis - not as new independent trigger conditions with
    their own threshold (no historical baseline exists yet to judge "unusual" demand against).
    """
    active = [f for f in other_findings if f.severity in ("watch", "notable")]
    if len(active) < 2:
        return []

    # Which finding(s) actually named each region, from each finding's own structured `regions`
    # field - populated by each rule from its real underlying data, not by scanning rendered
    # display text (which broke once findings started capping long lists, e.g. the PASA one
    # only showing its 8 largest windows - the other 124 could easily involve regions never
    # mentioned in the visible text). Empty regions on a finding (gas spread, coal decline) means
    # it's genuinely NEM-wide, not that detection missed something.
    # Prefer each finding's per-region detail (e.g. "12 of the 133 windows affect NSW1") over
    # its flat headline (e.g. "133 declared outage window(s)...") - the headline count would
    # otherwise get repeated identically against every region, which is misleading when one
    # region has a handful and another has dozens.
    region_reasons: dict[str, list[str]] = {}
    for f in active:
        for region in f.regions:
            region_reasons.setdefault(region, []).append(f.region_detail.get(region, f.headline))
    regions_involved = set(region_reasons)

    nem_wide = [f.headline for f in active if not f.regions]

    lines = [f"{len(active)} independent precursor patterns are active at once:"]
    for f in active:
        lines.append(f"  - {f.headline}")

    demand = signals.get("current_demand", {})
    wind_solar = signals.get("wind_solar_output_pct", {})
    weather = signals.get("weather_forecast", {})
    if regions_involved:
        lines.append("\nCurrent conditions - shown for each region a pattern above actually named:")
        for region in sorted(regions_involved):
            bits = []
            if demand.get(region) is not None:
                bits.append(f"demand {demand[region]:,.0f}MW")
            if wind_solar.get(region) is not None:
                bits.append(f"wind/solar {wind_solar[region]:.0f}%")
            context = f" ({', '.join(bits)})" if bits else ""
            line = f"  {region} - named by: {' & '.join(region_reasons[region])}{context}"

            # Tomorrow's forecast, always shown - not hidden behind a threshold, same principle
            # as everywhere else in this system (show the raw number, let the reader judge).
            # Consequence spelled out explicitly (-> higher price risk) rather than a bare label
            # like "low wind generation risk" that doesn't say what that actually implies.
            # Kept on the same line as the "named by" text (not its own line) - this block is
            # already the single biggest contributor to the pushed message's size.
            fc = weather.get(region)
            if fc and fc.get("dates"):
                idx = 1 if len(fc["dates"]) > 1 else 0  # index 1 = tomorrow (0 = today, already partly elapsed)
                max_t = fc["max_temp"][idx] if idx < len(fc.get("max_temp", [])) else None
                min_t = fc["min_temp"][idx] if idx < len(fc.get("min_temp", [])) else None
                wind = fc["max_wind_kmh"][idx] if idx < len(fc.get("max_wind_kmh", [])) else None
                temp_str = f"{min_t:.0f}-{max_t:.0f}C" if min_t is not None and max_t is not None else "n/a"
                wind_str = f"{wind:.0f}km/h wind" if wind is not None else "n/a"
                risk_notes = []
                if max_t is not None and max_t >= 33:
                    risk_notes.append("very hot->aircon demand->higher price risk")
                if min_t is not None and min_t <= 6:
                    risk_notes.append("very cold->heating demand->higher price risk")
                if wind is not None and wind <= 12:
                    risk_notes.append("light wind->less cheap supply->higher price risk")
                risk_str = f" [{'; '.join(risk_notes)}]" if risk_notes else ""
                line += f"; tomorrow {temp_str}, {wind_str}{risk_str}"
            lines.append(line)
    if nem_wide:
        lines.append("\n(" + " & ".join(nem_wide) + " - NEM-wide, not tied to a specific region.)")

    return [Finding(
        headline=f"{len(active)} precursor patterns active at once - compound risk building",
        detail="\n".join(lines),
        precedent="Real QED-documented crises rarely came from one signal alone - the 2022 crisis was a "
                  "gas price shock, coal outages, and a cold snap converging at the same time, not any "
                  "single one of those in isolation. Multiple independent patterns firing together is a "
                  "stronger signal than any one alone, even if each individually only sits at 'watch'. "
                  "Not a forecast - a pattern match against history, same as everything else here.",
        severity="notable",
    )]


def run_all(signals: dict) -> list[Finding]:
    findings = []
    for rule in ALL_RULES:
        try:
            findings.extend(rule(signals))
        except Exception as exc:
            print(f"[causal_rules] WARNING: rule {rule.__name__} failed: {exc}")

    try:
        findings.extend(rule_compound_risk(signals, findings))
    except Exception as exc:
        print(f"[causal_rules] WARNING: rule_compound_risk failed: {exc}")

    return findings
