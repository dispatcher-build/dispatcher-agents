"""Keyless mention sources: Hacker News (Algolia), Reddit (public JSON search)
and news (Google News RSS). Every item is normalized to one shape; cursors are
per source+keyword epochs so re-scans never re-fetch old mentions, even across
restarts."""

import html
import re
import time
from calendar import timegm
from urllib.parse import quote

import feedparser
import requests

HN_API = "https://hn.algolia.com/api/v1/search_by_date"
REDDIT_RSS = "https://www.reddit.com/search.rss?q={q}&sort=new&t=week&type=link"
NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
USER_AGENT = "dispatcher-brand-mention-monitor/1.0 (https://dispatcher.build)"
TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text or "")).strip()


def _hn_search(keyword: str, since_epoch: float) -> list:
    params = {
        "query": f'"{keyword}"',
        "tags": "(story,comment)",
        "numericFilters": f"created_at_i>{int(since_epoch)}",
        "hitsPerPage": 100,
    }
    resp = requests.get(HN_API, params=params, timeout=20, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    items = []
    for hit in resp.json().get("hits", []):
        object_id = str(hit.get("objectID"))
        thread_id = hit.get("story_id") or object_id
        is_story = "story" in (hit.get("_tags") or [])
        items.append({
            "id": f"hn:{object_id}",
            "source": "hn",
            "where": "Hacker News",
            "type": "story" if is_story else "comment",
            "title": hit.get("title") or hit.get("story_title") or "(untitled)",
            "url": hit.get("url") or hit.get("story_url") or f"https://news.ycombinator.com/item?id={object_id}",
            "link": f"https://news.ycombinator.com/item?id={thread_id}",
            "score": hit.get("points") or 0,
            "num_comments": hit.get("num_comments") or 0,
            "author": hit.get("author") or "",
            "created_at_i": hit.get("created_at_i") or 0,
            "keyword": keyword,
            "excerpt": _strip_html(hit.get("comment_text") or hit.get("story_text") or "")[:500],
        })
    return items


SUBREDDIT_RE = re.compile(r"reddit\.com/(r/[^/]+)/")


def _reddit_search(keyword: str, since_epoch: float) -> list:
    """Reddit's JSON endpoints are OAuth-only now, but the RSS search still
    serves anonymous clients. RSS carries no score/comment counts, so Reddit
    mentions never trip the hot threshold — they surface as plain mentions."""
    resp = requests.get(
        REDDIT_RSS.format(q=quote(f'"{keyword}"')),
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries:
        entry_id = entry.get("id") or ""
        if not entry_id.startswith("t3_"):
            continue
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        created = timegm(published) if published else 0
        if created <= since_epoch:
            continue
        link = entry.get("link") or ""
        match = SUBREDDIT_RE.search(link)
        items.append({
            "id": f"reddit:{entry_id}",
            "source": "reddit",
            "where": match.group(1) if match else "reddit",
            "type": "post",
            "title": entry.get("title") or "(untitled)",
            "url": link,
            "link": link,
            "score": 0,
            "num_comments": 0,
            "author": (entry.get("author") or "").lstrip("/u/"),
            "created_at_i": created,
            "keyword": keyword,
            "excerpt": _strip_html(entry.get("summary") or "")[:500],
        })
    return items


def _news_search(keyword: str, since_epoch: float) -> list:
    resp = requests.get(
        NEWS_RSS.format(q=quote(f'"{keyword}" when:7d')),
        timeout=20,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for entry in feed.entries:
        published = entry.get("published_parsed")
        created = timegm(published) if published else 0
        if created <= since_epoch:
            continue
        outlet = (entry.get("source") or {}).get("title", "") or "news"
        link = entry.get("link") or ""
        items.append({
            "id": f"news:{entry.get('id') or link}",
            "source": "news",
            "where": outlet,
            "type": "article",
            "title": entry.get("title") or "(untitled)",
            "url": link,
            "link": link,
            "score": 0,
            "num_comments": 0,
            "author": outlet,
            "created_at_i": created,
            "keyword": keyword,
            "excerpt": _strip_html(entry.get("summary") or "")[:500],
        })
    return items


SEARCHERS = {"hn": _hn_search, "reddit": _reddit_search, "news": _news_search}


def scan(keywords: list, sources: list, cursors: dict, default_lookback_hours: float = 24.0) -> tuple:
    """Search every source for every keyword past its cursor.
    Returns (items oldest-first, new_cursors)."""
    now = time.time()
    items = []
    new_cursors = dict(cursors)
    for source in sources:
        search = SEARCHERS.get(source)
        if not search:
            continue
        for kw in keywords:
            key = f"{source}:{kw}"
            since = cursors.get(key) or (now - default_lookback_hours * 3600)
            try:
                hits = search(kw, since)
            except Exception:
                continue
            if hits:
                new_cursors[key] = max(h["created_at_i"] for h in hits)
            items.extend(hits)
    items.sort(key=lambda h: h["created_at_i"])
    return items, new_cursors
