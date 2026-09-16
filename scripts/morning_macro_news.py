"""
Daily morning macro/geopolitical/commodities news briefing, pushed to ntfy.

Pipeline: free RSS feeds -> Groq's free LLM API writes a layman's-terms summary
with likely causes/effects, plus a closing note on whether anything could
plausibly affect Australian (NEM) power prices -> push to ntfy.

Headlines are pulled since the last successful run (state/morning_macro_news_state.json),
not a fixed lookback window - so a missed run (a dead cron ping, a failed workflow) doesn't
silently lose that day's news once the next run only looks back a fixed number of hours.

Runs entirely on free tiers - no paid API key required:
- RSS feeds are public and free.
- Groq's API has a free tier (no credit card) - set GROQ_API_KEY.
- ntfy.sh is free - set NTFY_TOPIC_MACRO_NEWS to a private, unguessable topic.

Designed to run unattended in GitHub Actions, same pattern as this repo's
other scheduled scripts (repository_dispatch primary trigger via an external
cron-job.org pinger, `schedule:` cron as a low-frequency fallback).
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests

NTFY_BASE_URL = "https://ntfy.sh"
REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "nem-intelligence-system/1.0"

# Same live-scrape mechanism this repo already uses for coal/gas benchmarks
# (coal_price_tracker.py, gas_spread_tracker.py) - tradingeconomics.com's public overview
# pages render a plain HTML table (name -> price -> % change cells) per instrument, so a
# regex extraction per name is enough; no API key, same CFD-proxy caveat as those trackers.
TE_BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
# (tradingeconomics.com display name, our label, price prefix, price suffix, decimal places)
TE_PRICE_PAGES = {
    "https://tradingeconomics.com/crypto": [
        ("Bitcoin", "Bitcoin", "$", "", 0),
    ],
    "https://tradingeconomics.com/commodities": [
        ("Crude Oil", "WTI Crude Oil", "$", "/bbl", 2),
        ("Brent", "Brent Crude Oil", "$", "/bbl", 2),
        ("Gold", "Gold", "$", "/oz", 2),
        ("Silver", "Silver", "$", "/oz", 2),
    ],
    "https://tradingeconomics.com/currencies": [
        ("AUDUSD", "AUD/USD", "", "", 4),
    ],
}

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "state", "morning_macro_news_state.json")

# Fallback lookback used only when there's no record of a previous run (first-ever run, or
# the state file is somehow missing) - normally the cutoff is "since the last successful run"
# instead, so a missed run doesn't quietly lose a day's news once the next run only looks back
# a fixed window. Capped at 96h so a very long outage doesn't dump days of stale headlines at once.
DEFAULT_LOOKBACK_HOURS = 30
MAX_LOOKBACK_HOURS = 96
MAX_ITEMS_PER_FEED = 8


def load_last_run_at() -> datetime:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        last_run_at = datetime.fromisoformat(state["last_run_at"])
        if last_run_at.tzinfo is None:
            last_run_at = last_run_at.replace(tzinfo=timezone.utc)
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return datetime.now(timezone.utc) - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    earliest_allowed = datetime.now(timezone.utc) - timedelta(hours=MAX_LOOKBACK_HOURS)
    return max(last_run_at, earliest_allowed)


def save_last_run_at(when: datetime) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_run_at": when.isoformat()}, f, indent=2)

MACRO_GEO_FEEDS = [
    ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("US Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
]

MARKETS_FEEDS = [
    ("OilPrice.com", "https://oilprice.com/rss/main"),
    ("CNBC Markets", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("FXStreet", "https://www.fxstreet.com/rss"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]

SYSTEM_PROMPT = """You write a short daily morning briefing for a reader with no finance or \
economics background. You will be given recent headlines grouped as "Macro/Geopolitical" and \
"Markets". Using ONLY these headlines (don't invent facts beyond what's implied by them):

1. Write a "Global Macro & Geopolitical" section: 3-5 bullet points on the most significant \
developments, each in plain layman's language, stating the likely CAUSE and the likely EFFECT \
(e.g. "X happened, because of Y, which could lead to Z").
2. Write a "Markets" section systematically covering EVERY global asset class present in the \
headlines, checking each of these in turn - global equities/stock markets (US, Europe, Asia, \
emerging markets), fixed income/bonds and interest rates, currencies/FX, commodities (oil, gas, \
coal, metals, agriculture), cryptocurrency, and real estate/REITs. Give commodities the most \
detail/space since that's the priority, but actively look for and report news in EVERY other \
asset class too - don't stop at whichever ones happen to dominate the headline list. Use one \
short labelled bullet per asset class that has real news, same cause/effect, layman's style. \
Only skip an asset class entirely if there is genuinely nothing about it in the headlines given.
3. End with a short "Australian power prices (NEM)" paragraph: plainly say whether anything \
above could plausibly flow through to Australian wholesale electricity prices, and briefly why \
(e.g. gas/coal price links, LNG export parity, a weather/demand angle, a currency effect). If \
nothing above is plausibly relevant, say so directly in one line rather than forcing a connection.

Keep the whole thing under 400 words, no preamble, no markdown headers with #, just plain text \
with short section titles in capitals and dashes for bullets. This is going straight into a \
push notification, so be concise."""


def extract_te_price(html: str, name: str) -> tuple[str, str] | None:
    """Pulls (price, pct_change) for one instrument out of a tradingeconomics.com overview
    page's HTML table. Returns None if that name isn't found or the table layout changed."""
    idx = html.find(f">{name}</b>")
    if idx == -1:
        return None
    window = html[idx:idx + 1500]
    price_match = re.search(r'id="p"[^>]*>\s*([\-\d,.]+)\s*</td>', window)
    pct_match = re.search(r'id="pch"[^>]*data-value="(-?[\d.]+)"', window)
    if not price_match:
        return None
    price = price_match.group(1)
    pct = f"{pct_match.group(1)}%" if pct_match else "?"
    return price, pct


def fetch_price_snapshot() -> list[str]:
    """Live price snapshot (Bitcoin, oil, gold, silver, AUD/USD) scraped from
    tradingeconomics.com - same mechanism as coal_price_tracker.py/gas_spread_tracker.py.
    Never raises; a failed page just means those lines are missing from the snapshot, not a
    dead briefing."""
    lines = []
    for url, instruments in TE_PRICE_PAGES.items():
        try:
            resp = requests.get(url, headers={"User-Agent": TE_BROWSER_USER_AGENT}, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[morning_macro_news] WARNING: price snapshot fetch failed for {url}: {exc}")
            continue
        for te_name, label, prefix, suffix, decimals in instruments:
            result = extract_te_price(resp.text, te_name)
            if result is None:
                print(f"[morning_macro_news] WARNING: could not find {te_name!r} on {url}")
                continue
            price_str, pct = result
            try:
                price_fmt = f"{float(price_str.replace(',', '')):,.{decimals}f}"
            except ValueError:
                price_fmt = price_str
            lines.append(f"{label}: {prefix}{price_fmt}{suffix} ({pct})")
    return lines


def fetch_feed_items(name: str, url: str, cutoff: datetime) -> list[str]:
    """Fetch and parse one RSS feed, returning headline lines published since `cutoff`. Never
    raises - one dead feed shouldn't take down the whole briefing."""
    lines = []
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
        for item in root.iter("item"):
            title_el = item.find("title")
            if title_el is None or not (title_el.text or "").strip():
                continue
            title = title_el.text.strip()

            pub_date_el = item.find("pubDate")
            if pub_date_el is not None and pub_date_el.text:
                try:
                    pub_date = parsedate_to_datetime(pub_date_el.text)
                    if pub_date.tzinfo is None:
                        pub_date = pub_date.replace(tzinfo=timezone.utc)
                    if pub_date < cutoff:
                        continue
                except (TypeError, ValueError):
                    pass  # keep items whose date we can't parse rather than dropping them

            lines.append(f"- {title}")
            if len(lines) >= MAX_ITEMS_PER_FEED:
                break
    except (requests.RequestException, ElementTree.ParseError) as exc:
        print(f"[morning_macro_news] WARNING: feed {name!r} failed: {exc}")
    return lines


def build_headline_block(cutoff: datetime) -> tuple[str, int]:
    """Returns the combined prompt text and how many headlines were actually collected."""
    sections = []
    total = 0
    for label, feeds in (("Macro/Geopolitical", MACRO_GEO_FEEDS), ("Markets", MARKETS_FEEDS)):
        block_lines = []
        for name, url in feeds:
            items = fetch_feed_items(name, url, cutoff)
            total += len(items)
            if items:
                block_lines.append(f"{name}:")
                block_lines.extend(items)
        sections.append(f"=== {label} ===\n" + "\n".join(block_lines) if block_lines
                         else f"=== {label} ===\n(no fresh headlines)")
    return "\n\n".join(sections), total


def summarize_with_groq(headline_block: str, api_key: str) -> str:
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": headline_block},
            ],
            "temperature": 0.4,
            "max_tokens": 2000,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def push_ntfy_attachment(topic: str, filename: str, html_content: str, short_message: str, title: str) -> None:
    """
    Pushes the full briefing as a downloadable .html attachment rather than the notification
    body text. The ntfy Android app has a known bug (github.com/binwiederhier/ntfy issue #1515)
    that crops/truncates long notification body text in its own in-app message view, even
    though the server and web app both handle the full ~4096-byte body fine.

    Also sets a "Click" URL pointing at the topic's own ntfy.sh web view. Some ntfy Android
    versions fail to open a downloaded attachment locally ("no installed app can open the
    file") even though the exact same file opens fine when the URL is pasted into a browser
    directly - confirmed live on 2026-09-14. Click uses ntfy's normal "open this URL in the
    browser" tap action instead of its local-file-open code path, so tapping the notification
    itself (not the attachment chip) reliably opens the message - including the attachment -
    in the browser regardless of that bug. `short_message` is the one-line text people see
    before opening the attachment.
    """
    url = f"{NTFY_BASE_URL.rstrip('/')}/{topic}"
    headers = {
        "User-Agent": USER_AGENT,
        "Title": title,
        "Priority": "3",
        "Tags": "newspaper",
        "Filename": filename,
        "Message": short_message,
        "Click": f"{NTFY_BASE_URL.rstrip('/')}/{topic}",
    }
    try:
        requests.post(url, data=html_content.encode("utf-8"), headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        print(f"[morning_macro_news] WARNING: ntfy attachment push failed: {exc}")


def summary_to_html(summary: str, title: str) -> str:
    import html
    escaped = html.escape(summary)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; line-height: 1.5; max-width: 700px;
        margin: 24px auto; padding: 0 16px; white-space: pre-wrap; }}
h1 {{ font-size: 1.2rem; }}
</style></head>
<body><h1>{html.escape(title)}</h1>{escaped}</body></html>"""


def main() -> int:
    groq_api_key = os.environ.get("GROQ_API_KEY", "").strip()
    ntfy_topic = os.environ.get("NTFY_TOPIC_MACRO_NEWS", "").strip()
    if not groq_api_key or not ntfy_topic:
        print("[morning_macro_news] ERROR: GROQ_API_KEY and NTFY_TOPIC_MACRO_NEWS must both be set.")
        return 1

    run_started_at = datetime.now(timezone.utc)
    cutoff = load_last_run_at()
    print(f"[morning_macro_news] Pulling headlines since {cutoff.isoformat()} "
          f"({(run_started_at - cutoff).total_seconds() / 3600:.1f}h ago).")

    headline_block, total_headlines = build_headline_block(cutoff)
    print(f"[morning_macro_news] Collected {total_headlines} fresh headlines.")
    if total_headlines == 0:
        print("[morning_macro_news] No fresh headlines from any feed - skipping today's briefing.")
        save_last_run_at(run_started_at)
        return 0

    try:
        summary = summarize_with_groq(headline_block, groq_api_key)
    except requests.RequestException as exc:
        print(f"[morning_macro_news] ERROR: Groq summarization failed: {exc}")
        return 1

    price_lines = fetch_price_snapshot()
    print(f"[morning_macro_news] Fetched {len(price_lines)} live prices.")
    if price_lines:
        summary = "CURRENT LEVELS\n" + "\n".join(f"- {line}" for line in price_lines) + "\n\n" + summary

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"Morning Macro Briefing - {today}"
    html_content = summary_to_html(summary, title)
    push_ntfy_attachment(
        ntfy_topic, filename=f"morning-briefing-{today}.html", html_content=html_content,
        short_message="Tap to read today's macro/geopolitical/commodities briefing",
        title=title,
    )
    print("[morning_macro_news] Briefing sent.")
    print(summary.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8"))
    save_last_run_at(run_started_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
