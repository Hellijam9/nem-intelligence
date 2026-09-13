"""
Daily morning macro/geopolitical/commodities news briefing, pushed to ntfy.

Pipeline: free RSS feeds -> Groq's free LLM API (Llama 3.3 70B) writes a
layman's-terms summary with likely causes/effects, plus a closing note on
whether anything could plausibly affect Australian (NEM) power prices ->
push to ntfy.

Runs entirely on free tiers - no paid API key required:
- RSS feeds are public and free.
- Groq's API has a free tier (no credit card) - set GROQ_API_KEY.
- ntfy.sh is free - set NTFY_TOPIC_MACRO_NEWS to a private, unguessable topic.

Designed to run unattended in GitHub Actions, same pattern as this repo's
other scheduled scripts (repository_dispatch primary trigger via an external
cron-job.org pinger, `schedule:` cron as a low-frequency fallback).
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests

NTFY_BASE_URL = "https://ntfy.sh"
REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "nem-intelligence-system/1.0"

# ntfy silently converts a push into an unreadable file attachment past ~4096 bytes
# (confirmed in nemweb_common.py) - stay well under it for UTF-8 headroom.
NTFY_MAX_MESSAGE_BYTES = 3900

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Only headlines newer than this are included, so a stalled/slow-updating feed
# doesn't quietly re-feed yesterday's items into today's briefing.
MAX_HEADLINE_AGE_HOURS = 30
MAX_ITEMS_PER_FEED = 8

MACRO_GEO_FEEDS = [
    ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("US Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
]

COMMODITY_FEEDS = [
    ("OilPrice.com", "https://oilprice.com/rss/main"),
    ("MarketWatch Top Stories", "https://www.marketwatch.com/rss/topstories"),
    ("MarketWatch Market Pulse", "https://www.marketwatch.com/rss/marketpulse"),
]

SYSTEM_PROMPT = """You write a short daily morning briefing for a reader with no finance or \
economics background. You will be given recent headlines grouped as "Macro/Geopolitical" and \
"Commodities". Using ONLY these headlines (don't invent facts beyond what's implied by them):

1. Write a "Global Macro & Geopolitical" section: 3-5 bullet points on the most significant \
developments, each in plain layman's language, stating the likely CAUSE and the likely EFFECT \
(e.g. "X happened, because of Y, which could lead to Z").
2. Write a "Commodities" section covering the most notable moves across asset classes \
(oil, gas, coal, metals, agriculture, etc. - whatever the headlines actually cover), same \
cause/effect, layman's style.
3. End with a short "Australian power prices (NEM)" paragraph: plainly say whether anything \
above could plausibly flow through to Australian wholesale electricity prices, and briefly why \
(e.g. gas/coal price links, LNG export parity, a weather/demand angle, a currency effect). If \
nothing above is plausibly relevant, say so directly in one line rather than forcing a connection.

Keep the whole thing under 350 words, no preamble, no markdown headers with #, just plain text \
with short section titles in capitals and dashes for bullets. This is going straight into a \
push notification, so be concise."""


def fetch_feed_items(name: str, url: str) -> list[str]:
    """Fetch and parse one RSS feed, returning recent headline+summary lines. Never raises -
    one dead feed shouldn't take down the whole briefing."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_HEADLINE_AGE_HOURS)
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


def build_headline_block() -> tuple[str, int]:
    """Returns the combined prompt text and how many headlines were actually collected."""
    sections = []
    total = 0
    for label, feeds in (("Macro/Geopolitical", MACRO_GEO_FEEDS), ("Commodities", COMMODITY_FEEDS)):
        block_lines = []
        for name, url in feeds:
            items = fetch_feed_items(name, url)
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
            "max_tokens": 900,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def push_ntfy(topic: str, message: str, title: str) -> None:
    body = message.encode("utf-8")
    if len(body) > NTFY_MAX_MESSAGE_BYTES:
        marker = f"\n...(truncated - {len(body)} bytes total)"
        keep = body[:NTFY_MAX_MESSAGE_BYTES - len(marker.encode("utf-8"))]
        while keep:
            try:
                keep.decode("utf-8")
                break
            except UnicodeDecodeError:
                keep = keep[:-1]
        body = keep + marker.encode("utf-8")
        print(f"[morning_macro_news] WARNING: message was {len(message.encode('utf-8'))} bytes, truncated.")

    url = f"{NTFY_BASE_URL.rstrip('/')}/{topic}"
    headers = {"User-Agent": USER_AGENT, "Title": title, "Priority": "3", "Tags": "newspaper"}
    try:
        requests.post(url, data=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        print(f"[morning_macro_news] WARNING: ntfy push failed: {exc}")


def main() -> int:
    groq_api_key = os.environ.get("GROQ_API_KEY", "").strip()
    ntfy_topic = os.environ.get("NTFY_TOPIC_MACRO_NEWS", "").strip()
    if not groq_api_key or not ntfy_topic:
        print("[morning_macro_news] ERROR: GROQ_API_KEY and NTFY_TOPIC_MACRO_NEWS must both be set.")
        return 1

    headline_block, total_headlines = build_headline_block()
    print(f"[morning_macro_news] Collected {total_headlines} fresh headlines.")
    if total_headlines == 0:
        print("[morning_macro_news] No fresh headlines from any feed - skipping today's briefing.")
        return 0

    try:
        summary = summarize_with_groq(headline_block, groq_api_key)
    except requests.RequestException as exc:
        print(f"[morning_macro_news] ERROR: Groq summarization failed: {exc}")
        return 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    push_ntfy(ntfy_topic, summary, title=f"Morning Macro Briefing - {today}")
    print("[morning_macro_news] Briefing sent.")
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
