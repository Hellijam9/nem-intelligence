"""
Shared helpers for the NEM Intelligence System.

Every script in this system follows the same pattern:
    1. Discover the latest file(s) on a NEMWEB CURRENT report directory
    2. Download and parse the MMS-format CSV inside the zip
    3. Enrich rows with the local DUID registry (station name, owner, fuel, capacity)
    4. Push an alert to ntfy if something crossed a threshold

This module implements steps 1, 2, 3 and the ntfy push, so individual scripts
only need to contain their own alerting logic.
"""

from __future__ import annotations

import csv
import inspect
import io
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import pandas as pd
import requests

# AEMO "NEM time" is fixed Australia/Brisbane (UTC+10), no daylight saving -
# same as Sydney's non-DST offset, close enough for a 7pm-7am quiet-hours
# window (DST would shift Sydney's actual wall-clock by up to an hour, not
# worth the added complexity for a "roughly overnight" cutoff).
NEM_TZ = timezone(timedelta(hours=10))

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "registry"
STATE_DIR = PROJECT_ROOT / "state"
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"

STATE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "ntfy_base_url": "https://ntfy.sh",
    "request_timeout_seconds": 30,
    "user_agent": "nem-intelligence-system/1.0",
}


def load_config() -> dict:
    """Load config/config.json, falling back to defaults for anything missing.

    ntfy_topics is treated separately: on a public GitHub repo, committing the real
    topic strings in config.json would let anyone browsing the repo find them (ntfy
    topics are unauthenticated - knowing the string is enough to read or spoof alerts).
    If NTFY_TOPICS_JSON is set (a GitHub Actions repository secret), it overrides
    whatever ntfy_topics config.json has, so the real values never need to be
    committed at all. Falls back to config.json's own values for local/laptop runs.
    """
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[nemweb_common] WARNING: could not read {CONFIG_PATH}: {exc}")

    topics_override = os.environ.get("NTFY_TOPICS_JSON")
    if topics_override:
        try:
            cfg["ntfy_topics"] = json.loads(topics_override)
        except json.JSONDecodeError as exc:
            print(f"[nemweb_common] WARNING: NTFY_TOPICS_JSON env var is not valid JSON: {exc}")

    return cfg


CONFIG = load_config()
HTTP_HEADERS = {"User-Agent": CONFIG["user_agent"]}


# ---------------------------------------------------------------------------
# NEMWEB directory discovery + download
# ---------------------------------------------------------------------------

_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)


def list_nemweb_files(directory_url: str, filename_pattern: str) -> list[str]:
    """
    Fetch a NEMWEB CURRENT directory listing page and return full URLs of
    every file whose name matches `filename_pattern` (a regex), sorted
    ascending by filename. Since NEMWEB filenames embed zero-padded
    date/time stamps, sorting the filename string sorts chronologically.

    NEMWEB's directory listing hrefs are absolute paths (e.g.
    "/Reports/CURRENT/DispatchIS_Reports/PUBLIC_DISPATCHIS_....zip"), not
    bare filenames, so we match the pattern against the basename and resolve
    the full URL with urljoin (handles both relative and absolute hrefs).
    """
    resp = None
    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(directory_url, headers=HTTP_HEADERS, timeout=CONFIG["request_timeout_seconds"])
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 3:
                print(f"[nemweb_common] {directory_url} attempt {attempt}/3 failed ({exc}); retrying...")
                time.sleep(2.0 * attempt)
    if resp is None:
        raise last_exc  # type: ignore[misc]

    pattern = re.compile(filename_pattern)
    matches: dict[str, str] = {}  # basename -> resolved absolute URL
    for href in _HREF_RE.findall(resp.text):
        basename = href.rsplit("/", 1)[-1]
        if pattern.search(basename):
            matches[basename] = urljoin(directory_url, href)

    return [matches[name] for name in sorted(matches)]


def get_latest_files(directory_url: str, filename_pattern: str, n: int = 1) -> list[str]:
    """Convenience wrapper: return the last `n` matching files (most recent last)."""
    files = list_nemweb_files(directory_url, filename_pattern)
    if not files:
        raise RuntimeError(f"No files matching {filename_pattern!r} found at {directory_url}")
    return files[-n:]


def download_bytes(url: str, retries: int = 3, backoff_seconds: float = 2.0) -> bytes:
    """
    GET a URL and return the response body. NEMWEB occasionally drops the
    connection mid-handshake on larger files (SSL EOF errors) - retry with
    a short backoff before giving up, rather than letting one flaky request
    kill an unattended cron run.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=CONFIG["request_timeout_seconds"])
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                print(f"[nemweb_common] {url} attempt {attempt}/{retries} failed ({exc}); retrying...")
                time.sleep(backoff_seconds * attempt)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# High Impact Outages (transmission/interconnector planned outages)
# ---------------------------------------------------------------------------
#
# AEMO publishes a weekly "High Impact Outages" CSV covering planned network
# (not generator) outages - transmission lines, interconnectors - genuinely
# out to ~10 years ahead in places, unlike STPASA's INTERCONNECTORSOLN table
# (~7 days only). No MMS wrapper here, no ZIP - a plain CSV, cp1252-encoded
# (not UTF-8 - it has literal bullet characters), with embedded newlines
# inside quoted cells (date + day-name on two lines, multi-line Impact text).
#
# The interconnector affected is only ever named in the free-text "Impact"
# column, not a clean structured field, so this is keyword-matched against
# the interconnector labels this project already uses elsewhere.

HIGH_IMPACT_OUTAGES_URL = "https://www.nemweb.com.au/Reports/CURRENT/HighImpactOutages/"
HIGH_IMPACT_OUTAGES_PATTERN = r"^High_Impact_Outages_(\d{8})\.csv$"

INTERCONNECTOR_KEYWORDS = {
    "NSW1-QLD1": ["qni", "queensland - new south wales", "new south wales - queensland"],
    "N-Q-MNSP1": ["terranora"],
    "VIC1-NSW1": ["victoria - new south wales", "new south wales - victoria"],
    "V-SA": ["heywood"],
    "V-S-MNSP1": ["murraylink"],
    "T-V-MNSP1": ["basslink"],
}


def fetch_high_impact_outages() -> pd.DataFrame:
    """
    Latest High Impact Outages CSV, cleaned and with an `_interconnectors` column added
    (list of this project's interconnector IDs whose keywords matched the Impact text for
    that row - empty list if the outage doesn't mention one of the 6 tracked here).

    Picks the latest file by parsed date, not just "last when filenames are sorted" - a stray
    implausibly-far-future-dated file has been observed in this directory (confirmed live,
    2026-08-25), which would otherwise get treated as "the latest" if sorting on the filename
    string alone. Only dates within a week of today are considered plausible.
    """
    files = list_nemweb_files(HIGH_IMPACT_OUTAGES_URL, HIGH_IMPACT_OUTAGES_PATTERN)
    if not files:
        raise RuntimeError(f"No High Impact Outages CSV found at {HIGH_IMPACT_OUTAGES_URL}")

    today = datetime.now().date()
    dated = []
    for url in files:
        m = re.search(HIGH_IMPACT_OUTAGES_PATTERN, url.rsplit("/", 1)[-1])
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if abs((file_date - today).days) <= 7:
            dated.append((file_date, url))
    if not dated:
        raise RuntimeError("No plausibly-recent High Impact Outages CSV found (all candidates too far from today's date)")
    dated.sort()
    latest_url = dated[-1][1]

    raw = download_bytes(latest_url).decode("cp1252", errors="replace")
    df = pd.read_csv(io.StringIO(raw), on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]

    for col in ("Start", "Finish"):
        if col in df.columns:
            df[col] = df[col].fillna("").str.split("\n").str[0].str.strip()

    if "Status" in df.columns:
        df = df[df["Status"].fillna("") != "Withdrawn"].copy()

    impact_lower = df.get("Impact", pd.Series(dtype=str)).fillna("").str.lower()
    df["_interconnectors"] = [
        [ic_id for ic_id, keywords in INTERCONNECTOR_KEYWORDS.items() if any(k in text for k in keywords)]
        for text in impact_lower
    ]
    return df


# ---------------------------------------------------------------------------
# MMS CSV parsing
# ---------------------------------------------------------------------------
#
# AEMO's MMS CSV format packs multiple tables into one file:
#   C,... header/footer rows (comment) -> ignore
#   I,<PACKAGE>,<TABLE>,<VERSION>,<col1>,<col2>,...   -> defines columns for the table that follows
#   D,<PACKAGE>,<TABLE>,<VERSION>,<val1>,<val2>,...   -> a data row for the most recently defined table
#
# A single file can define+fill several tables in sequence (e.g. DispatchIS
# contains PRICE, INTERCONNECTORRES, REGIONSUM, and more). We key each table
# by (PACKAGE, TABLE) and expose a fuzzy lookup so callers can ask for
# "DISPATCHPRICE" or "DISPATCH_UNIT_SCADA" without worrying about the exact
# underscore/concatenation AEMO used for a given report.


def _normalise(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def parse_mms_csv(text: str) -> dict[tuple[str, str], pd.DataFrame]:
    """Parse raw MMS CSV text into {(package, table): DataFrame}."""
    tables: dict[tuple[str, str], list[list[str]]] = {}
    columns: dict[tuple[str, str], list[str]] = {}

    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row:
            continue
        record_type = row[0].strip()
        if record_type == "I":
            key = (row[1].strip(), row[2].strip())
            columns[key] = [c.strip() for c in row[4:]]
            tables.setdefault(key, [])
        elif record_type == "D":
            key = (row[1].strip(), row[2].strip())
            if key not in columns:
                # Data row arrived without a preceding header row - skip, malformed.
                continue
            tables.setdefault(key, []).append(row[4:])

    frames: dict[tuple[str, str], pd.DataFrame] = {}
    for key, rows in tables.items():
        cols = columns.get(key, [])
        # Pad/truncate rows to match column count defensively - NEMWEB files
        # occasionally add trailing columns between versions.
        width = len(cols)
        clean_rows = [r[:width] + [""] * (width - len(r)) for r in rows]
        frames[key] = pd.DataFrame(clean_rows, columns=cols)
    return frames


def parse_mms_zip(zip_bytes: bytes) -> dict[tuple[str, str], pd.DataFrame]:
    """Unzip a NEMWEB report (single CSV inside) and parse it."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError("No CSV file found inside zip")
        with zf.open(csv_names[0]) as f:
            text = f.read().decode("utf-8", errors="replace")
    return parse_mms_csv(text)


def get_table(tables: dict[tuple[str, str], pd.DataFrame], table_name: str) -> pd.DataFrame:
    """
    Fuzzy lookup: `table_name` can be given as "DISPATCHPRICE",
    "DISPATCH_UNIT_SCADA", "PRICE", etc. - matches against the
    normalised (package+table) or table-only string.
    """
    target = _normalise(table_name)
    for (package, table), df in tables.items():
        combined = _normalise(package + table)
        table_only = _normalise(table)
        if target == combined or target == table_only or target in combined:
            return df
    available = [f"{p}.{t}" for p, t in tables.keys()]
    raise KeyError(f"Table {table_name!r} not found. Available tables: {available}")


def download_and_get_table(zip_url: str, table_name: str) -> pd.DataFrame:
    """One-shot: download a NEMWEB zip and return the requested table as a DataFrame."""
    tables = parse_mms_zip(download_bytes(zip_url))
    return get_table(tables, table_name)


# ---------------------------------------------------------------------------
# ASX $300 Cap contract settlement math
# ---------------------------------------------------------------------------
#
# Straight from ASX's "Australian Electricity Derivatives" product fact sheet
# (contract spec for the Base Load Calendar Quarter $300 Cap Futures Contract):
#
#   Cash Settlement Price = (C - (300 x D)) / E
#     C = sum of all spot prices in the period that are greater than $300
#     D = count of intervals in the period with a spot price greater than $300
#     E = count of ALL intervals in the period (not just the ones above $300)
#
#   Cash Settlement Value = Cash Settlement Price x total MWh in the period
#
# This "Price" is a $/MWh figure - what a quarterly $300 cap CONTRACT settles
# at - distinct from "payout" (the Value, the actual $ a 1MW holder receives).
# Algebraically, Value reduces to sum((price-300).clip(0)) * interval_hours,
# which is what this project's cap scripts already computed correctly before
# this was added - only the Price side (the $/MWh average using ASX's exact
# E-in-the-denominator convention) was missing.


def cap_settlement(prices, strike: float, interval_hours: float) -> tuple[float, float]:
    """
    Returns (cap_price, cap_value) for a series of spot prices over some
    period, per the ASX $300 Cap contract settlement formula. `prices` is
    any iterable/Series of numeric $/MWh values (already coerced to float -
    caller is responsible for pd.to_numeric upstream). `interval_hours` is
    the length of one interval in hours (5/60 for DispatchIS, 0.5 for
    Predispatch, etc).
    """
    prices = pd.Series(prices).dropna()
    e = len(prices)
    if e == 0:
        return 0.0, 0.0
    exceeding = prices[prices > strike]
    c = exceeding.sum()
    d = len(exceeding)
    cap_price = (c - strike * d) / e
    total_mwh = e * interval_hours
    cap_value = cap_price * total_mwh
    return float(cap_price), float(cap_value)


def quarter_bounds(d) -> tuple:
    """
    Calendar-quarter start/end (exclusive) and label for a given datetime,
    matching ASX's Base Load Calendar Quarter contract months (Mar/Jun/Sep/Dec).
    Shared by cap_quarter_to_date.py and cap_dayahead.py so both scripts agree
    on exactly where a quarter starts and ends.
    """
    from datetime import datetime as _dt
    q_start_month = ((d.month - 1) // 3) * 3 + 1
    start = _dt(d.year, q_start_month, 1)
    end_month = q_start_month + 3
    end = _dt(d.year + 1, 1, 1) if end_month > 12 else _dt(d.year, end_month, 1)
    label = f"Q{(q_start_month - 1) // 3 + 1} {d.year}"
    return start, end, label


# ---------------------------------------------------------------------------
# Registry enrichment
# ---------------------------------------------------------------------------


@dataclass
class Registry:
    duid_info: Optional[pd.DataFrame]          # DUID, REGION, UNIT_NAME
    owner_capacity: Optional[pd.DataFrame]      # DUID, Owner, Number of Units, Nameplate Capacity (MW) - summed per DUID
    fuel_info: Optional[pd.DataFrame]           # DUID, REGIONID, PORTFOLIO, STATIONNAME, FUEL, TransmissionLossFactor, CAPACITY
    merged: Optional[pd.DataFrame]              # left-joined on DUID, or None if nothing loaded

    def enrich(self, df: pd.DataFrame, duid_col: str = "DUID") -> pd.DataFrame:
        """Left-join registry fields onto a dataframe that has a DUID column."""
        if self.merged is None or duid_col not in df.columns:
            return df
        return df.merge(self.merged, how="left", left_on=duid_col, right_on="DUID", suffixes=("", "_reg"))


_REGISTRY_FILENAMES = {
    "duid_info": "duid_info.csv",
    "owner_capacity": "duid_owner_units_capacity.csv",
    "fuel_info": "Scheduled+Plant+Information+Fuel(-2).csv",
}


def load_registry() -> Registry:
    """
    Load the three registry CSVs from registry/. Any file that doesn't exist
    yet is skipped with a one-line warning rather than raising - scripts
    should still run (unenriched) before the real registry files are supplied.
    """
    duid_info = owner_capacity = fuel_info = None

    path = REGISTRY_DIR / _REGISTRY_FILENAMES["duid_info"]
    if path.exists():
        duid_info = pd.read_csv(path)
    else:
        print(f"[nemweb_common] NOTE: {path.name} not found in registry/ - DUID enrichment (region/unit name) disabled until supplied.")

    path = REGISTRY_DIR / _REGISTRY_FILENAMES["owner_capacity"]
    if path.exists():
        raw = pd.read_csv(path)
        # Known bug in the source build doc: this file has one row per unit
        # (multiple rows per DUID). Sum capacity and units per DUID instead
        # of dropping duplicates, which silently discards MW.
        capacity_col = next((c for c in raw.columns if "capacity" in c.lower()), None)
        units_col = next((c for c in raw.columns if "number of units" in c.lower()), None)
        owner_col = next((c for c in raw.columns if c.lower() == "owner"), None)
        agg = {}
        if capacity_col:
            agg[capacity_col] = "sum"
        if units_col:
            agg[units_col] = "sum"
        if owner_col:
            agg[owner_col] = "first"
        owner_capacity = raw.groupby("DUID", as_index=False).agg(agg) if agg else raw.drop_duplicates(subset="DUID")
    else:
        print(f"[nemweb_common] NOTE: {path.name} not found in registry/ - owner/capacity enrichment disabled until supplied.")

    path = REGISTRY_DIR / _REGISTRY_FILENAMES["fuel_info"]
    if path.exists():
        fuel_info = pd.read_csv(path)
    else:
        print(f"[nemweb_common] NOTE: {path.name} not found in registry/ - fuel-type enrichment disabled until supplied.")

    merged = None
    for part in (duid_info, fuel_info, owner_capacity):
        if part is None or "DUID" not in part.columns:
            continue
        merged = part if merged is None else merged.merge(part, how="outer", on="DUID", suffixes=("", "_dup"))

    return Registry(duid_info=duid_info, owner_capacity=owner_capacity, fuel_info=fuel_info, merged=merged)


# ---------------------------------------------------------------------------
# ntfy push
# ---------------------------------------------------------------------------

NOTIFICATION_LOG_FILE = "notification_log.jsonl"
NOTIFICATION_LOG_RETENTION_DAYS = 30


def _log_notification(topic: str, title: Optional[str], message: str) -> None:
    """
    Appends every ntfy push to a shared log so the morning/weekend recap can
    later answer "what fired since the last recap" - push_ntfy() itself never
    persisted anything before this, so there was nothing for a recap to read.
    Source script is inferred from the caller's filename, not passed in, so
    every existing push_ntfy() call site needed zero changes.
    """
    caller = inspect.stack()[2]
    source = Path(caller.filename).stem
    entry = {
        "ts": datetime.now(NEM_TZ).isoformat(),
        "source": source,
        "topic": topic,
        "title": title or "",
        "message": message,
    }
    path = STATE_DIR / NOTIFICATION_LOG_FILE
    cutoff = datetime.now(NEM_TZ) - timedelta(days=NOTIFICATION_LOG_RETENTION_DAYS)
    kept = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                old = json.loads(line)
                if datetime.fromisoformat(old["ts"]) >= cutoff:
                    kept.append(line)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    kept.append(json.dumps(entry))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_notification_log(since: Optional[datetime] = None) -> list[dict]:
    """Returns logged notifications (as dicts), optionally only those at/after `since`."""
    path = STATE_DIR / NOTIFICATION_LOG_FILE
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["ts"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if since is not None and ts < since:
            continue
        entries.append(entry)
    entries.sort(key=lambda e: e["ts"])
    return entries


# ntfy.sh silently converts a push into a file attachment (title/body replaced with "You
# received a file: attachment.txt") once the body exceeds its message-size-limit - confirmed
# live at exactly 4096 bytes (4000 stays a normal message, 4096 already becomes an attachment).
# requests never raises on this (still HTTP 200), so without a guard here a script can "succeed"
# while the actual push on your phone is unreadable. Stay well under it for UTF-8 headroom.
NTFY_MAX_MESSAGE_BYTES = 3900


def push_ntfy(topic: str, message: str, title: Optional[str] = None,
              priority: Optional[str] = None, tags: Optional[list[str]] = None) -> None:
    """
    Push a message to an ntfy topic. Priority: min/low/default/high/urgent.
    Tags are ntfy's emoji-shortcode annotations, e.g. ["warning", "zap"].
    Never raises on failure - an alerting script failing to alert should log
    the problem, not crash the whole cron job.
    """

    body = message.encode("utf-8")
    if len(body) > NTFY_MAX_MESSAGE_BYTES:
        marker = f"\n...(truncated - {len(body)} bytes total, see local script output for the rest)"
        keep = body[:NTFY_MAX_MESSAGE_BYTES - len(marker.encode("utf-8"))]
        while keep:
            try:
                keep.decode("utf-8")
                break
            except UnicodeDecodeError:
                keep = keep[:-1]
        body = keep + marker.encode("utf-8")
        print(f"[nemweb_common] WARNING: message to {topic!r} was {len(message.encode('utf-8'))} bytes "
              f"(over ntfy's {NTFY_MAX_MESSAGE_BYTES}-byte budget) - truncated before sending.")

    url = f"{CONFIG['ntfy_base_url'].rstrip('/')}/{topic}"
    headers = dict(HTTP_HEADERS)
    if title:
        headers["Title"] = title
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        requests.post(url, data=body, headers=headers,
                       timeout=CONFIG["request_timeout_seconds"])
    except requests.RequestException as exc:
        print(f"[nemweb_common] WARNING: ntfy push to {topic!r} failed: {exc}")
    try:
        _log_notification(topic, title, message)
    except Exception as exc:
        print(f"[nemweb_common] WARNING: failed to log notification: {exc}")


# ---------------------------------------------------------------------------
# Small state-file helpers (JSON, atomic-ish write)
# ---------------------------------------------------------------------------


def read_state(name: str, default=None):
    path = STATE_DIR / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def write_state(name: str, value) -> None:
    path = STATE_DIR / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, default=str, indent=2))
    tmp.replace(path)
