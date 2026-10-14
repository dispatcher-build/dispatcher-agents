"""Page fetching, text extraction and diffing for the site monitor agent."""

import difflib
import hashlib
import re

import httpx
from bs4 import BeautifulSoup
from dispatchkit import netsafe

UA = "Mozilla/5.0 (compatible; DispatcherSiteMonitor/1.0; +https://dispatcher.build)"
MAX_DIFF_LINES = 80


def url_key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def fetch_text(url: str, selector: str = "") -> tuple:
    """Return (status_code, normalized_text). status 0 = network error."""
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=20, follow_redirects=True,
                          event_hooks={"request": [netsafe.httpx_guard]}) as client:
            resp = client.get(url)
    except Exception:
        return 0, ""
    if resp.status_code != 200:
        return resp.status_code, ""

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    node = (soup.select_one(selector) if selector else None) or soup.body or soup
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in node.get_text("\n").splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return 200, text


def change_ratio(old: str, new: str) -> float:
    if not old and not new:
        return 0.0
    return 1.0 - difflib.SequenceMatcher(None, old, new).ratio()


def unified_diff(old: str, new: str, url: str) -> str:
    diff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"{url} (before)", tofile=f"{url} (after)", lineterm="", n=1,
    ))
    if len(diff) > MAX_DIFF_LINES:
        diff = diff[:MAX_DIFF_LINES] + [f"... ({len(diff) - MAX_DIFF_LINES} more lines)"]
    return "\n".join(diff)
