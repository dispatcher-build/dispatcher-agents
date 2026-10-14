"""Fetch vendor health from Atlassian Statuspage-compatible status sites.

Every vendor is a base URL (e.g. https://www.githubstatus.com); the summary
endpoint at /api/v2/summary.json carries both the overall indicator and the
list of unresolved incidents.
"""

import requests

TIMEOUT = 15
HEADERS = {"User-Agent": "dispatcher-agent (+https://dispatcher.build)"}


def parse_vendors(raw: str) -> list:
    """Parse 'Name|https://status.example.com,...' into [{name, url}]."""
    vendors = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            name, url = chunk.split("|", 1)
        else:
            name, url = chunk, chunk
        url = url.strip().rstrip("/")
        if not url.startswith("http"):
            url = "https://" + url
        vendors.append({"name": name.strip() or url, "url": url})
    return vendors


def fetch_summary(base_url: str) -> dict:
    """One vendor's health: overall indicator plus unresolved incidents."""
    resp = requests.get(f"{base_url}/api/v2/summary.json", headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status") or {}
    incidents = []
    for inc in data.get("incidents") or []:
        incidents.append({
            "id": str(inc.get("id", "")),
            "name": inc.get("name", ""),
            "status": inc.get("status", ""),
            "impact": inc.get("impact", ""),
            "url": inc.get("shortlink") or base_url,
            "updated_at": inc.get("updated_at", ""),
        })
    return {
        "indicator": status.get("indicator", "unknown"),
        "description": status.get("description", ""),
        "incidents": incidents,
    }
