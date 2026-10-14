"""Feed fetching and normalization for the news digest agent."""

import hashlib
import re
import time

import feedparser

TAG_RE = re.compile(r"<[^>]+>")


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html or "")).strip()


def fetch(feed_urls: list) -> list:
    """Fetch and normalize entries from every feed; bad feeds are skipped."""
    items = []
    for url in feed_urls:
        parsed = feedparser.parse(url)
        source = parsed.feed.get("title", url) if parsed.feed else url
        for entry in parsed.entries:
            link = entry.get("link", "")
            uid = entry.get("id") or link or entry.get("title", "")
            if not uid:
                continue
            published = ""
            for key in ("published_parsed", "updated_parsed"):
                if entry.get(key):
                    published = time.strftime("%Y-%m-%dT%H:%M:%S", entry[key])
                    break
            items.append({
                "id": hashlib.sha1(uid.encode()).hexdigest()[:16],
                "title": _clean(entry.get("title", "")),
                "link": link,
                "summary": _clean(entry.get("summary", ""))[:500],
                "source": source,
                "published": published,
            })
    return items


def matched_keywords(item: dict, keywords: list) -> list:
    haystack = f"{item['title']} {item['summary']}".lower()
    return [k for k in keywords if k in haystack]
