"""GitHub API helpers with conditional requests (304s are free quota-wise)."""

import httpx

API = "https://api.github.com"
UA = "DispatcherRepoRadar/1.0 (+https://dispatcher.build)"


def _get(path: str, token: str = "", etag: str = "") -> tuple:
    """Return (status, json_or_none, etag)."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    try:
        resp = httpx.get(f"{API}{path}", headers=headers, timeout=20, follow_redirects=True)
    except Exception:
        return 0, None, etag
    if resp.status_code == 304:
        return 304, None, etag
    new_etag = resp.headers.get("ETag", "")
    return resp.status_code, (resp.json() if resp.status_code == 200 else None), new_etag


def snapshot(repo: str, token: str, etags: dict) -> dict:
    """Fetch current repo stats + latest release. Mutates etags in place."""
    out = {"repo": repo}

    status, data, etag = _get(f"/repos/{repo}", token, etags.get(f"repo:{repo}", ""))
    etags[f"repo:{repo}"] = etag
    if status == 200 and data:
        out["stats"] = {
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "open_issues": data.get("open_issues_count", 0),
            "pushed_at": data.get("pushed_at", ""),
        }
    elif status not in (304, 0):
        out["error"] = f"HTTP {status}"

    status, data, etag = _get(f"/repos/{repo}/releases/latest", token, etags.get(f"rel:{repo}", ""))
    etags[f"rel:{repo}"] = etag
    if status == 200 and data:
        out["release"] = {
            "tag": data.get("tag_name", ""),
            "name": data.get("name") or data.get("tag_name", ""),
            "published_at": data.get("published_at", ""),
            "url": data.get("html_url", ""),
            "notes": (data.get("body") or "")[:1500],
        }
    return out
