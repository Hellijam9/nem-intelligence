# NEM Intelligence System

Python scripts polling public AEMO NEMWEB data, pushing alerts to your phone via [ntfy](https://ntfy.sh). All rebuilt fresh on Windows (not Mac, despite the original build doc) and live-tested against real NEMWEB data during development.

## Setup

1. Python 3.12 is installed at `C:\Users\helli\AppData\Local\Programs\Python\Python312`. Open a **new** terminal (so PATH picks it up) and confirm:
   ```
   python --version
   ```
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Everything reads `config/config.json` for thresholds, regions, and ntfy topics - see below before running anything.

## Project structure

```
nem-intelligence/
  scripts/          all the .py files - run from inside this folder (they import nemweb_common as a sibling module)
  config/config.json
  registry/         put duid_info.csv, duid_owner_units_capacity.csv, and the Scheduled Plant Information Fuel CSV here
  state/            auto-created; JSON/CSV state files each script uses to avoid duplicate alerts / build history
```

## Scripts

| Script | What it does | Cadence |
|---|---|---|
| `nemweb_common.py` | Shared module - not run directly | - |
| `pasa_monitor.py` | Diffs MTPASA snapshots, alerts on 100MW+ declared-availability changes, grouped into outage windows | ~3h |
| `cap_dayahead.py` | Expected cap payout for *today* (actual-so-far from DispatchIS + forecast remainder from Predispatch) and *tomorrow* (full-day forecast), each also projected against the running quarter-to-date total from `cap_quarter_to_date.py`. Replaces the old GitHub Actions version that used a Neopoint feed you no longer have access to. Self-contained by design, so it runs correctly on GitHub Actions too - see `.github/workflows/cap_payout_alert.yml`. Only notifies per-region if that region's today/tomorrow figure moved >=10% since the last notification (or a payout newly appeared/vanished entirely) - compared against the last *notified* value, not just the last run, so slow drift still eventually crosses the threshold. Pushed message is trimmed to only the region(s) that actually triggered it | 30 min (local) / on-demand (GitHub Actions) |
| `cap_quarter_to_date.py` | **New.** The genuine running quarterly cap payout (applied to every settled day since the calendar quarter started), replacing the old flat "×90 days" guess. No standalone "yesterday" script anymore - `cap_yesterday.py` was removed since its whole output duplicated this script's own figure. Resets automatically each new quarter (Mar/Jun/Sep/Dec). Only notifies if the figure changed since last run | daily, ~5:02am |
| `spot_spike.py` | Alerts when any region's spot price crosses $300/$1,000/$5,000/$15,000 - debounced by tier, alerts on both crossing up into a higher tier and dropping back down (including a full drop back below every threshold, ending the episode) | 5 min |
| `interconnector_monitor.py` | Alerts when an interconnector hits ≥90% of its flow limit (hysteresis reset at 80%) | 5 min |
| `scada_drop_monitor.py` | Rebid Stage 1 - alerts on 100MW+ output drops, grouped by owner, logs to `state/scada_drops.csv` for next-day reconciliation | 5 min |
| `rebid_reconciler.py` | Rebid Stage 2 - matches yesterday's drops against rebid explanation text, grouped by the registry's readable owner name (not AEMO's raw PARTICIPANTID code) | daily, ~5am |
| `predispatch_tracker.py` | Alerts if any region's forecast price exceeds $300, as far out as the current Predispatch run covers (~31h typically) - not a fixed short window. Re-alerts if a period's forecast later moves >=10% (up or down) or drops back under $300, not just the first time it crosses | 30 min |
| `coal_fleet_trend.py` | **New.** Daily aggregate coal availability trend (needs fuel registry for a true coal-only figure). Only notifies if the figure changed since last recorded - comparisons only ever match against a same-fuel-filter-setting entry, so an old unfiltered baseline can't get compared against a filtered one | daily, ~6am |
| `negative_pricing_tracker.py` | **New.** Daily negative-price-frequency trend by region (trailing 7-day window each run) - the physical signal behind the battery cap-erosion thesis. Only notifies if a region's percentage changed | daily, ~6:05am |
| `gas_spread_tracker.py` | **New.** Domestic gas hub prices (STTM + Wallumbilla) vs international LNG - both sides now fetched live (JKM scraped from tradingeconomics.com, AUD/USD from frankfurter.app), falling back to config.json's manual values only if a live fetch fails. Reports unconditionally every day (no alert threshold here anymore) - `causal_rules.py`'s `rule_gas_spread` is what judges whether a given day's spread is actually notable (threshold $15/GJ, grounded in the real 2021-22 lead-up range) | daily |
| `closure_watcher.py` | **New.** Scans AEMO's Market Notice feed for closure/retirement keywords (Eraring-relevant) | daily |
| `customer_watcher.py` | **New.** Real-time MW moves (±20MW default) on a specific customer/portfolio's own DUIDs, resolved from `duid_owner_units_capacity.csv`'s Owner column via `customer_portfolios` in config - no separate DUID list needed. Message is grouped by owner (all of one company's moves together), not a flat list | 5 min |
| `causal_rules.py` | **New.** Not run directly - the QED historical mechanics (spot/contract drivers, the eight regime patterns) encoded as pattern-matching rules that `market_read.py` runs over live signals | - |
| `market_read.py` | **New - the cause-and-effect synthesis layer.** Pulls fresh live spot prices + interconnector flows + demand + wind/solar output %, plus every other script's state (cap payout, active spike tiers, today's SCADA drops, negative-pricing trend, coal-fleet trend, gas spread, upcoming PASA windows) and a 3-day weather forecast per region, prints it all as raw data, then runs `causal_rules.py` over it and appends a "this matches pattern X from the QED history" commentary on top - including a compound-risk synthesis when 2+ independent patterns are active at once. Also includes a regional outlook block (SA bullish/QLD bearish standing view + NSW/VIC "coiled spring" calls that flip to RESOLVED the moment `closure_watcher.py` actually catches an Eraring/Loy Yang A closure notice). This is the live equivalent of what the QED report does for history. | 4x daily: 7am, 12pm, 2pm, 6pm |
| `reserve_outlook.py` | **New.** There's no public AEMO price forecast beyond Predispatch's ~2-day horizon - this isn't one either. It's AEMO's own official region-level reserve-adequacy computation (`Short_Term_PASA_Reports` → `REGIONSOLUTION`, ~7 days ahead, 30-min granularity) - the exact mechanism behind real AEMO Lack-of-Reserve notices, with plain-English explanations of what LOR1/2/3 actually mean when one fires. Shows each of the next ~7 days' tightest (minimum) surplus reserve per region. Fills the 2-7 day gap between Predispatch (price) and PASA (outages) with a forward *risk* signal, not a dollar figure. Second section: diffs the two latest `STPASA_DUIDAvailability` snapshots (same ≥100MW/grouped-by-DUID style as `pasa_monitor.py`) for DUID-level *attribution* - the region reserve numbers already reflect any DUID change, this just tells you which generator caused it. Only notifies if something actually changed since last run | daily, ~6:10am |

## Cap payout - what's actually calculated

The 2 remaining cap scripts (`cap_dayahead.py`, `cap_quarter_to_date.py`) compute one thing: **payout** ($) - for every interval where price is above $300, take the excess over $300, multiply by the interval's length in hours, and add it up. Divided by 24 (per your confirmation that's the correct figure). This is the dollar amount a holder of **1 MW** of a $300 cap contract actually receives - multiply by your real position size for the real figure.

No cap price ($/MWh, the ASX Cash Settlement Price formula) is shown anywhere - your old GitHub scripts never computed one, and you confirmed you don't want it added back in. Only the $ payout, matching the original method exactly.

They differ only in *which* stretch of prices each one adds up:
- `cap_dayahead.py` - "Today" = actual so far + forecast for the rest of today (this is the one running most frequently, so it covers the "since midnight" ground `cap_intraday.py` used to). "Tomorrow" = full-day forecast. Both projected against the running quarter-to-date baseline from `cap_quarter_to_date.py`.
- `cap_quarter_to_date.py` - every settled day since the quarter started, added together.

(`cap_intraday.py` was removed - its "today so far, actual only" figure was redundant with `cap_dayahead.py`'s "Today" once you decided to keep the full-day figure instead.)

Every "$ adds to qrtr" figure divides by the *actual* number of days in the current quarter (`nemweb_common.quarter_bounds`, 90/91/92 depending which quarter), not a flat 90 - so it's a genuine like-for-like share, and the running quarter-to-date total is always just the sum of each day's own share (checkable by hand).

**Registry files are all in and confirmed working** - station names, owners, and fuel-type filtering are live (verified: `scada_drop_monitor.py` now correctly excludes wind/solar; `pasa_monitor.py` shows real station/owner names).

**Dropped from the original spec (verified, not just assumed):** the "NemPriceSetter... updates every 5 min" data source in the build doc doesn't exist on NEMWEB's real-time tier. Checked live: AEMO's NEMDE solution files (`Data_Archive/Wholesale_Electricity/NEMDE/`) only publish **monthly**, ~2 weeks after month-end, in ~5GB zips per month. There's no free way to know which DUID set the price in real time. `customer_watcher.py` covers the MW-movement half of the original "customer" script; true price-setter alerting would need a paid data feed.

## What's still missing

1. **Set `customer_portfolios` in `config/config.json`** - currently empty (`[]`). Add owner-name substrings (e.g. `"Origin Energy"`, `"Snowy Hydro Ltd"`) whenever you know which portfolio(s) to watch - matches directly against the registry you already provided, no new file needed. Tested working with a temporary `["Origin Energy"]` value (resolved 26 real DUIDs, caught a live +253MW move on the Eraring battery) then reverted to empty.
2. **International LNG benchmark** (`gas_spread_tracker.py`) and **AUD/USD rate** are manually maintained in `config/config.json` under `gas_benchmark` - there's no free automated feed for either. Update `last_updated` when you refresh them; the script flags itself as stale after 14 days.
3. **DWGM (Victorian) gas price** isn't covered by `gas_spread_tracker.py` - only STTM (Sydney/Brisbane/Adelaide) + Wallumbilla were found and confirmed working in the time available.
4. **No watchdog for silent script failures.** ntfy only fires when a script finds something to alert on - if a script crashes under Task Scheduler (bad NEMWEB response, etc.), nothing currently tells you.
5. **Most scripts aren't set up on GitHub Actions** - only `cap_dayahead.py` is (see below), since the others need persistent state to function correctly, not just to run.

## Quiet hours - built, then removed (2026-08-20)

A 7pm-7am quiet-hours system was built and tested (queuing every script's notifications overnight into one 7am consolidated digest via `overnight_digest.py`), then removed entirely at your request the same night. Every script now sends immediately again, always, exactly as it did before quiet hours existed - `nemweb_common.push_ntfy()` has no time-of-day gating, `overnight_digest.py` and its scheduled task are deleted, and `state/overnight_alert_queue.json` no longer exists.

## ntfy topics (subscribe to these in the ntfy app)

These are randomised (not the generic names from the original build doc) so nothing crosses over to anyone who might know the old topic names:

- `nem-pasa-CHANGE_ME` - PASA changes, coal fleet trend, negative pricing trend, closure watcher (grouped: low-frequency/structural)
- `nem-cappayouts-CHANGE_ME` - cap payout day-ahead + quarter-to-date
- `nem-rebid-CHANGE_ME` - SCADA drops + next-day reconciliation
- `nem-spot-CHANGE_ME` - spot price spikes
- `nem-interconnector-CHANGE_ME` - interconnector constraints
- `nem-predispatch-CHANGE_ME` - forward price forecast breaches
- `nem-gasspread-CHANGE_ME` - gas benchmark spread
- `nem-customer-CHANGE_ME` - customer/portfolio DUID moves
- `nem-marketread-CHANGE_ME` - the cause-and-effect synthesis digest
- `nem-reserveoutlook-CHANGE_ME` - **new, not one of the original topics - subscribe to this separately** - the 7-day reserve outlook (`reserve_outlook.py`)

Change the `CHANGE_ME` suffix in `config/config.json` any time you want to rotate to a fresh set of private topic names.

## Scheduling on Windows (Task Scheduler, not cron)

**Live now.** All 15 scripts are running unattended via Windows Task Scheduler (`NEM-*` tasks), created with `schtasks` (the command-line equivalent of the build doc's suggested cron schedule). Verified genuinely running under the scheduler, not just interactively. All tasks run via `pythonw.exe` (not `python.exe`) so nothing pops up a console window when they fire - same scripts, same schedule, just windowless.

All tasks run as **SYSTEM** (not your own account) - after your own account's stored credential twice went stale for unattended firing (`0x80070520`, no clear cause), you decided reliability mattered more than the least-privilege tradeoff. SYSTEM needs no stored password, so it can't go stale the same way. If you ever need to change this back, see `switch_tasks_to_system.ps1` for the pattern (needs a genuinely admin-elevated PowerShell window - a plain one that looks the same will silently fail with "Access is denied").

**Note: every cadence below was my own judgment call while building each script, not something you specified per-script - worth reviewing and adjusting (see open item in `STATUS.md`).**

| Cadence | Tasks |
|---|---|
| Every 5 min | `NEM-SpotSpike`, `NEM-Interconnector`, `NEM-ScadaDrop`, `NEM-CustomerWatcher` |
| Every 30 min | `NEM-CapDayAhead`, `NEM-Predispatch` |
| 4x daily (7am/12pm/2pm/6pm) | `NEM-MarketRead` (changed from every 30 min, 2026-08-20) |
| Every 3 hours | `NEM-Pasa` |
| Daily, ~5am | `NEM-CapQuarterToDate` (~5:02, so `cap_dayahead.py`'s baseline is fresh), `NEM-RebidReconciler`, `NEM-GasSpread`, `NEM-ClosureWatcher` |
| Daily, ~6:10am | `NEM-ReserveOutlook` |
| Daily, ~6am | `NEM-CoalFleetTrend` (6:00), `NEM-NegativePricing` (6:05) - originally weekly Tuesdays, changed to daily 2026-08-20 |

Example of the underlying command (note: `--%` is used to stop PowerShell's own argument parsing, since the "qed archive" folder name has a space in it that otherwise corrupts the quoted `/TR` value):

```
schtasks --% /Create /TN "NEM-SpotSpike" /TR "\"C:\Users\helli\AppData\Local\Programs\Python\Python312\pythonw.exe\" \"C:\Users\helli\Downloads\qed archive\nem-intelligence\scripts\spot_spike.py\"" /SC MINUTE /MO 5 /ST 00:00 /F
```

To inspect, pause, or delete any of them: open Task Scheduler and look under the root folder for anything starting `NEM-`, or `schtasks /Query /FO TABLE | findstr NEM-`.

Note: `market_read.py` reads state written by several other scripts (cap payout, spot spike, negative pricing, coal fleet, gas spread, PASA windows) - it degrades gracefully (shows "no data yet") for anything that hasn't run yet, but is most useful once everything's had at least one cycle.

Tasks run as your interactive user, only while logged on (no `/RU` specified) - they'll survive a screen lock but not a full logout/restart until you're logged back in.

## GitHub Actions (alternative to local Task Scheduler)

`cap_dayahead.py` also has a GitHub Actions workflow (`.github/workflows/cap_payout_alert.yml`), updated from an older version that called a now-dead Neopoint-based script. Unlike the Task Scheduler jobs above, GitHub Actions runners are stateless - a fresh VM every run, nothing persists between runs unless you commit it back to the repo. `cap_dayahead.py`'s core actual+forecast figures were specifically designed to be self-contained (it re-fetches and recomputes everything from scratch each run), so those work correctly there without any extra plumbing.

One exception: the quarter-projection figures (added after the initial GitHub Actions build) read `cap_quarter_to_date.py`'s state file as a baseline - on a fresh GitHub Actions VM that file won't exist, so `cap_dayahead.py` degrades gracefully (treats the baseline as empty/zero, with a note in the output) rather than failing. The quarter projection is only meaningful when run where `cap_quarter_to_date.py`'s state persists, i.e. locally via Task Scheduler.

Only `cap_dayahead.py` is set up this way so far. Most of the other scripts (PASA diff, spot-spike debounce, SCADA-drop dedup, the trend trackers) rely on state genuinely persisting run-to-run, which won't work as-is on GitHub Actions without adding a "commit state back to the repo" step first - not built, since local Task Scheduler already covers them and they need that persistence to function correctly (not just to avoid duplicate alerts).
