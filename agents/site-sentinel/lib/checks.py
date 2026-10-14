"""Availability, latency, TLS certificate and DNS checks. httpx plus stdlib —
no third-party monitoring service, no API key."""

import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from dispatchkit import netsafe

UA = "Mozilla/5.0 (compatible; DispatcherSiteSentinel/1.0; +https://dispatcher.build)"


def hostname_of(url: str) -> str:
    return urlparse(url).hostname or ""


def check_http(url: str, must_contain: str = "", timeout: float = 20.0) -> dict:
    """Fetch a URL and report health. status 0 means the request never
    completed (DNS failure, refused connection, timeout)."""
    started = time.monotonic()
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=timeout, follow_redirects=True,
                          event_hooks={"request": [netsafe.httpx_guard]}) as client:
            resp = client.get(url)
    except Exception as e:
        return {
            "ok": False, "status": 0, "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(e).__name__}: {e}"[:200], "content_ok": False,
        }

    latency_ms = int((time.monotonic() - started) * 1000)
    status_ok = 200 <= resp.status_code < 400
    content_ok = (must_contain.lower() in resp.text.lower()) if must_contain else True
    error = ""
    if not status_ok:
        error = f"HTTP {resp.status_code}"
    elif not content_ok:
        error = f"page loaded but the expected text {must_contain!r} was missing"

    return {
        "ok": status_ok and content_ok, "status": resp.status_code,
        "latency_ms": latency_ms, "error": error, "content_ok": content_ok,
    }


def check_cert(url: str, timeout: float = 10.0) -> dict:
    """TLS certificate expiry for an https URL. Returns {} for http URLs."""
    if not url.lower().startswith("https://"):
        return {}
    host = hostname_of(url)
    port = urlparse(url).port or 443
    try:
        netsafe.resolve_public_host(host)
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:200]}

    expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    issuer = ""
    for part in cert.get("issuer", ()):
        for key, value in part:
            if key == "organizationName":
                issuer = value
    return {
        "expires": expires.isoformat(),
        "days_left": (expires - datetime.now(timezone.utc)).days,
        "issuer": issuer,
    }


def check_dns(url: str) -> dict:
    host = hostname_of(url)
    try:
        return {"resolves": True, "ips": netsafe.resolve_public_host(host)[:5]}
    except Exception as e:
        return {"resolves": False, "ips": [], "error": f"{type(e).__name__}: {e}"[:200]}


def run_checks(url: str, must_contain: str = "", timeout: float = 20.0) -> dict:
    """One full pass over a target: HTTP, and on failure DNS to tell apart
    'the host is gone' from 'the app is broken'."""
    result = {"url": url, "checked_at": datetime.now(timezone.utc).isoformat()}
    result.update(check_http(url, must_contain, timeout))
    if not result["ok"]:
        result["dns"] = check_dns(url)
    return result
