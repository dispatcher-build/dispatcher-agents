"""Outbound URL guard for user-configured targets.

Agents run inside the platform's network with open egress. A webhook or a
monitored page pointed at the ECS credential endpoint, the metadata service,
or a VPC-internal address would let an agent's config reach things its code
was never meant to see. `assert_public_url` rejects anything that does not
resolve to a public unicast address; `is_public_url` is the boolean form.
"""

import ipaddress
import os
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")
_BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal", "instance-data"}


class UnsafeUrl(ValueError):
    """The URL points somewhere agents must not reach."""


def _allow_private() -> bool:
    return os.environ.get("DISPATCHKIT_ALLOW_PRIVATE_URLS", "0") == "1"


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified or getattr(addr, "is_site_local", False)
    )


def resolve_public_host(host: str) -> list:
    """Resolve host and return its addresses, raising UnsafeUrl if any is non-public."""
    if not host or host.lower().rstrip(".") in _BLOCKED_HOSTS:
        raise UnsafeUrl(f"host {host!r} is not allowed")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeUrl(f"could not resolve {host}: {e}")
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise UnsafeUrl(f"{host} resolved to nothing")
    bad = [ip for ip in ips if not _is_public_ip(ip)]
    if bad:
        raise UnsafeUrl(f"{host} resolves to a non-public address ({bad[0]})")
    return ips


def assert_public_url(url: str) -> str:
    """Return url unchanged when it is http(s) to a public host; raise UnsafeUrl otherwise."""
    parts = urlsplit(url or "")
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrl(f"scheme {parts.scheme or '(none)'} is not allowed")
    if parts.username or parts.password:
        raise UnsafeUrl("credentials in URLs are not allowed")
    host = parts.hostname or ""
    if _allow_private():
        if not host:
            raise UnsafeUrl("URL has no host")
        return url
    resolve_public_host(host)
    return url


def is_public_url(url: str) -> bool:
    try:
        assert_public_url(url)
        return True
    except UnsafeUrl:
        return False


def httpx_guard(request) -> None:
    """httpx request event hook that re-checks every hop, redirects included."""
    assert_public_url(str(request.url))
