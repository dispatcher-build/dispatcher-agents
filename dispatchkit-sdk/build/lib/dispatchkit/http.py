"""
Guarded HTTP for agents: every request goes through the public-host check in
netsafe, gets a timeout, and retries transient failures with backoff. Uses the
standard library so agents need no extra dependency for plain fetches.
"""

import json as _json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from dispatchkit import netsafe

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 3
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
USER_AGENT = "dispatchkit-agent/1.0"


class HTTPError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body


@dataclass
class Response:
    status: int
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.text)


def fetch(
    url: str,
    *,
    method: str = "GET",
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    json: Any = None,
    data: Optional[bytes] = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = 1.5,
    raise_for_status: bool = True,
) -> Response:
    if params:
        joiner = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{joiner}{urllib.parse.urlencode(params, doseq=True)}"
    netsafe.assert_public_url(url)

    req_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    payload = data
    if json is not None:
        payload = _json.dumps(json).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=payload, headers=req_headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response = Response(resp.status, resp.geturl(), dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            response = Response(e.code, url, dict(e.headers.items()) if e.headers else {}, body)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt > retries:
                raise
            delay = backoff ** attempt
            logger.warning(f"fetch {url} failed ({e}); retry {attempt}/{retries} in {delay:.1f}s")
            time.sleep(delay)
            continue

        if response.status in RETRY_STATUSES and attempt <= retries:
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else backoff ** attempt
            except ValueError:
                delay = backoff ** attempt
            logger.warning(f"fetch {url} -> {response.status}; retry {attempt}/{retries} in {delay:.1f}s")
            time.sleep(min(delay, 60.0))
            continue

        if raise_for_status and not response.ok:
            raise HTTPError(response.status, url, response.text[:500])
        return response


def get_json(url: str, **kwargs) -> Any:
    return fetch(url, **kwargs).json()
