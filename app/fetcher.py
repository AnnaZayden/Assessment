"""Fetching untrusted URLs without letting callers reach internal networks.

Each hop (initial URL and every redirect):
  1. syntax check (scheme, port, no credentials),
  2. resolve the host ourselves and refuse if *any* address is non-public,
  3. connect to the exact IP we checked (Host header + TLS SNI keep the
     original name), so DNS cannot change between check and connect.
The body is streamed and abandoned as soon as it exceeds the size limit.

Why all this complexity?
Without it a caller could say "fetch http://192.168.1.1/secret" and the
service would happily reach their internal network. This is called SSRF
(Server-Side Request Forgery) — one of the most common cloud vulnerabilities.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .config import Settings
from .errors import ApiError, ErrorCode
from .urls import DEFAULT_PORTS, IPAddress, is_public_ip, url_problem

log = logging.getLogger(__name__)

Resolver = Callable[[str, int], Awaitable[list[str]]]

REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
USER_AGENT: Final = "OursImageService/1.0"
BYTES_PER_MB: Final = 1024 * 1024


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


@dataclass(frozen=True)
class FetchedImage:
    data: bytes
    content_type: str | None
    final_url: str


class Fetcher:
    def __init__(
        self,
        settings: Settings,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """``transport`` swaps the network for a fake one in tests; ``ssl_context`` changes trusted CAs."""
        self._settings = settings
        self._resolver = resolver
        self._client = httpx.AsyncClient(
            transport=transport,
            verify=ssl_context or True,
            # Don't let httpx follow redirects automatically — we re-check each redirect URL ourselves
            # to ensure no redirect leads to a private address.
            follow_redirects=False,
            # An environment proxy (HTTP_PROXY etc.) would route traffic around our IP-pinning check.
            trust_env=False,
            timeout=httpx.Timeout(settings.fetch_timeout_s, connect=settings.connect_timeout_s),
            # Keep-alive disabled: pooled connections are keyed by IP, so a connection opened
            # (and TLS-verified) for a.com could be reused for b.com if they share a CDN IP.
            # Disabling this ensures each connection goes to the IP we just verified.
            limits=httpx.Limits(max_keepalive_connections=0),
            headers={"User-Agent": USER_AGENT},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str, *, video: bool = False) -> FetchedImage:
        timeout = self._settings.fetch_timeout_s
        kind = "video" if video else "image"
        try:
            return await asyncio.wait_for(self._fetch(url, kind), timeout=timeout)
        except (TimeoutError, httpx.TimeoutException):
            raise ApiError(
                ErrorCode.UPSTREAM_TIMEOUT, f"Fetching the source {kind} took longer than {timeout:g}s."
            ) from None
        except httpx.HTTPError as exc:
            raise ApiError(
                ErrorCode.UPSTREAM_UNREACHABLE, f"Could not fetch the source {kind}: {type(exc).__name__}."
            ) from None

    async def _fetch(self, url: str, kind: str) -> FetchedImage:
        current = url
        for hop in range(self._settings.max_redirects + 1):
            request = await self._pinned_request(current, via_redirect=hop > 0)
            request.headers["Accept"] = f"{kind}/*"
            response = await self._client.send(request, stream=True)
            try:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise ApiError(
                            ErrorCode.UPSTREAM_ERROR, "Source sent a redirect without a Location header."
                        )
                    current = urljoin(current, location)
                    continue
                if not response.is_success:
                    raise ApiError(
                        ErrorCode.UPSTREAM_ERROR,
                        f"Source responded with HTTP {response.status_code}.",
                        upstream_status=response.status_code,
                    )
                data = await self._read_limited(response, kind)
                return FetchedImage(data, response.headers.get("content-type"), current)
            finally:
                await response.aclose()
        raise ApiError(
            ErrorCode.TOO_MANY_REDIRECTS, f"Source redirected more than {self._settings.max_redirects} times."
        )

    async def _pinned_request(self, url: str, *, via_redirect: bool) -> httpx.Request:
        """Build a request that connects directly to the resolved IP, not the hostname.

        IP pinning prevents a TOCTOU (time-of-check/time-of-use) attack where DNS could
        return a safe IP during our check but a private IP when we actually connect.
        We put the original hostname in the Host header and SNI so TLS verification and
        virtual hosting still work correctly despite connecting to the raw IP.
        """
        if problem := url_problem(url, self._settings):
            if via_redirect:
                raise ApiError(ErrorCode.URL_NOT_ALLOWED, f"Source redirected to a disallowed URL: {problem}")
            raise ApiError(ErrorCode.INVALID_URL, problem, param="url")

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname or ""
        port = parts.port or DEFAULT_PORTS[scheme]
        # Resolve and validate the IP before we connect to it.
        ip = await self._resolve_checked(host, port, via_redirect=via_redirect)

        ip_host = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
        netloc = ip_host if parts.port is None else f"{ip_host}:{port}"
        named_host = f"[{host}]" if ":" in host else host  # IPv6 literals keep their brackets in Host
        host_header = named_host if parts.port is None else f"{named_host}:{port}"
        # Replace the hostname in the URL with the raw IP so httpx connects to the IP we checked.
        pinned_url = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
        # For HTTPS, tell httpx to use the original hostname for TLS SNI (server name indication),
        # otherwise the certificate check would fail because it expects a hostname, not an IP.
        extensions = {"sni_hostname": host} if scheme == "https" else {}
        return self._client.build_request(
            "GET", pinned_url, headers={"Host": host_header}, extensions=extensions
        )

    async def _resolve_checked(self, host: str, port: int, *, via_redirect: bool) -> IPAddress:
        try:
            # If the host is already an IP literal (e.g. "1.2.3.4"), skip DNS entirely.
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                addresses = [ipaddress.ip_address(a) for a in await self._resolver(host, port)]
            except (OSError, ValueError) as exc:
                log.debug("dns lookup failed host=%s error=%r", host, exc)
                addresses = []
            if not addresses:
                raise ApiError(ErrorCode.DNS_ERROR, f"Could not resolve host '{host}'.") from None

        # Reject if *any* resolved address is internal — not just the first one.
        # A hostname that resolves to both a public and a private IP is suspicious (DNS rebinding attack).
        if not all(is_public_ip(a) for a in addresses):
            where = "Source redirected to" if via_redirect else "URL points to"
            raise ApiError(
                ErrorCode.URL_NOT_ALLOWED,
                f"{where} a private, loopback, or otherwise non-public address ({host}).",
                param=None if via_redirect else "url",
            )
        return addresses[0]

    async def _read_limited(self, response: httpx.Response, kind: str) -> bytes:
        limit = self._settings.max_video_bytes if kind == "video" else self._settings.max_source_bytes
        too_large = ApiError(
            ErrorCode.SOURCE_TOO_LARGE, f"Source {kind} is larger than the {_human_bytes(limit)} limit."
        )
        # Fast path: if Content-Length already tells us the file is too big, reject before downloading.
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise too_large
        buf = bytearray()
        # Stream the body in chunks and stop the moment we exceed the limit.
        # aiter_bytes() yields *decoded* bytes, so a gzip bomb (tiny compressed, huge decompressed) is also caught.
        async for chunk in response.aiter_bytes():
            buf += chunk
            if len(buf) > limit:
                raise too_large
        return bytes(buf)


def _human_bytes(n: int) -> str:
    return f"{n / BYTES_PER_MB:g} MB" if n >= BYTES_PER_MB else f"{n:,} bytes"
