"""Hacker News search via the Algolia API — no auth, JSON, generous limits."""

import html
import re
import time

import requests

API = "https://hn.algolia.com/api/v1/search_by_date"
TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(TAG_RE.sub(" ", text or "")).strip()


def _normalize(hit: dict, keyword: str) -> dict:
    object_id = str(hit.get("objectID"))
    is_story = "story" in (hit.get("_tags") or [])
    thread_id = hit.get("story_id") or object_id
    title = hit.get("title") or hit.get("story_title") or "(untitled)"
    text = hit.get("comment_text") or hit.get("story_text") or ""
    return {
        "id": object_id,
        "type": "story" if is_story else "comment",
        "title": title,
        "url": hit.get("url") or hit.get("story_url") or f"https://news.ycombinator.com/item?id={object_id}",
        "hn_link": f"https://news.ycombinator.com/item?id={thread_id}",
        "points": hit.get("points") or 0,
        "num_comments": hit.get("num_comments") or 0,
        "author": hit.get("author") or "",
        "created_at": hit.get("created_at") or "",
        "created_at_i": hit.get("created_at_i") or 0,
        "keyword": keyword,
        "excerpt": _strip_html(text)[:500],
    }


def search_since(keyword: str, since_epoch: float) -> list:
    """New stories and comments matching keyword, oldest first."""
    params = {
        "query": f'"{keyword}"',
        "tags": "(story,comment)",
        "numericFilters": f"created_at_i>{int(since_epoch)}",
        "hitsPerPage": 100,
    }
    resp = requests.get(API, params=params, timeout=20)
    resp.raise_for_status()
    hits = [_normalize(h, keyword) for h in resp.json().get("hits", [])]
    hits.sort(key=lambda h: h["created_at_i"])
    return hits


def scan(keywords: list, cursors: dict, default_lookback_hours: float = 24.0) -> tuple:
    """
    Search every keyword past its cursor. Returns (items, new_cursors).
    Cursor per keyword is the newest created_at_i seen, so re-scans never
    re-fetch old mentions even across restarts.
    """
    now = time.time()
    items = []
    new_cursors = dict(cursors)
    for kw in keywords:
        since = cursors.get(kw) or (now - default_lookback_hours * 3600)
        try:
            hits = search_since(kw, since)
        except requests.RequestException:
            continue
        if hits:
            new_cursors[kw] = max(h["created_at_i"] for h in hits)
        items.extend(hits)
    return items, new_cursors
