"""
RSS news scanning. Static industry feeds are matched against company names;
per-ticker feeds (URL contains {ticker}) are fetched for a rotating batch of
tickers each scan to stay polite.
"""

import hashlib
import logging

import feedparser

logger = logging.getLogger(__name__)

TICKER_BATCH_SIZE = 15


def _entry_id(entry) -> str:
    key = entry.get("link") or entry.get("title", "")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _match_company(title: str, name_map: dict) -> str:
    lowered = title.lower()
    for name, ticker in name_map.items():
        if name in lowered:
            return ticker
    return ""


def _keyword_hits(title: str, keywords: list) -> list:
    lowered = title.lower()
    return [k for k in keywords if k.lower() in lowered]


def build_name_map(metrics_by_ticker: dict) -> dict:
    """Lowercased distinctive company name -> ticker, for matching headlines."""
    name_map = {}
    for ticker, metrics in metrics_by_ticker.items():
        name = (metrics.get("name") or "").lower()
        for suffix in (" inc", " corp", " co", " plc", " ltd", " holdings", " technologies", " technology"):
            idx = name.find(suffix)
            if idx > 0:
                name = name[:idx]
                break
        name = name.strip().rstrip(",.")
        if len(name) >= 4:
            name_map[name] = ticker
    return name_map


def scan(
    feeds: list,
    tickers: list,
    name_map: dict,
    keywords: list,
    seen_ids: set,
    ticker_offset: int = 0,
) -> tuple:
    """
    Fetch feeds and return (items, next_ticker_offset).
    Each item: {"id", "ticker", "title", "link", "published"}.
    Only unseen entries with a resolved ticker and a trigger keyword are returned.
    """
    static_feeds = [f for f in feeds if "{ticker}" not in f]
    ticker_template = next((f for f in feeds if "{ticker}" in f), None)

    urls = [(url, "") for url in static_feeds]
    if ticker_template and tickers:
        batch = [tickers[(ticker_offset + i) % len(tickers)] for i in range(min(TICKER_BATCH_SIZE, len(tickers)))]
        urls += [(ticker_template.format(ticker=t), t) for t in batch]
        next_offset = (ticker_offset + len(batch)) % len(tickers)
    else:
        next_offset = ticker_offset

    items = []
    for url, feed_ticker in urls:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            logger.warning(f"Feed failed {url}: {e}")
            continue
        for entry in parsed.get("entries", [])[:25]:
            entry_id = _entry_id(entry)
            if entry_id in seen_ids:
                continue
            title = entry.get("title", "")
            ticker = feed_ticker or _match_company(title, name_map)
            hits = _keyword_hits(title, keywords)
            if not ticker or not hits:
                continue
            items.append({
                "id": entry_id,
                "ticker": ticker,
                "title": title,
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
            })

    return items, next_offset
