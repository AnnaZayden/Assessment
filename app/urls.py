"""Checks on which URLs and addresses we are willing to fetch."""

from __future__ import annotations

import ipaddress
from typing import Final
from urllib.parse import SplitResult, urlsplit

from .config import Settings

MAX_URL_LENGTH: Final = 2048
DEFAULT_PORTS: Final = {"http": 80, "https": 443}

_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def url_problem(url: str, settings: Settings) -> str | None:
    """Return a human-readable reason the URL is unacceptable, or None if it is fine.

    This is syntax only; the address the host resolves to is checked at fetch time.
    """
    if len(url) > MAX_URL_LENGTH:
        return f"URL is longer than {MAX_URL_LENGTH} characters."
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "URL could not be parsed."
    return _parts_problem(parts, port, settings)


def _parts_problem(parts: SplitResult, port: int | None, settings: Settings) -> str | None:
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        problem: str | None = "Only http:// and https:// URLs are supported."
    elif not parts.hostname:
        problem = "URL must include a host name."
    elif parts.username is not None or parts.password is not None:
        problem = "URLs with embedded credentials (user:pass@host) are not allowed."
    elif (effective_port := port or DEFAULT_PORTS[scheme]) not in settings.allowed_ports:
        allowed = ", ".join(str(p) for p in sorted(settings.allowed_ports))
        problem = f"Port {effective_port} is not allowed. Allowed ports: {allowed}."
    else:
        problem = None
    return problem


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses.

    Rejects loopback, RFC 1918, link-local (incl. cloud metadata 169.254.169.254),
    CGNAT, multicast, reserved, unspecified, and IPv6 forms that embed an IPv4
    address (mapped, 6to4, NAT64) when that embedded address is not public.
    The IPv6 unwrapping repeats what recent Python versions already do; it is kept
    so the guarantee does not depend on the interpreter version.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.teredo:
            return False
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_ip(embedded)
    return ip.is_global and not ip.is_multicast
