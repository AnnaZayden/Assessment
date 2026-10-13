"""Fetching untrusted URLs without letting callers reach internal networks.

Each hop (initial URL and every redirect):
  1. syntax check (scheme, port, no credentials),
  2. resolve the host ourselves and refuse if *any* address is non-public,
  3. connect to the exact IP we checked (Host header + TLS SNI keep the
     original name), so DNS cannot change between check and connect.
The body is streamed and abandoned as soon as it exceeds the size limit.
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
            follow_redirects=False,  # we follow manually so every hop is re-checked
            trust_env=False,  # an env proxy would bypass IP pinning
            timeout=httpx.Timeout(settings.fetch_timeout_s, connect=settings.connect_timeout_s),
            # No keep-alive: pooled connections are keyed by IP, so a connection
            # opened (and TLS-verified) for a.com could be reused for b.com on
            # the same CDN IP.
            limits=httpx.Limits(max_keepalive_connections=0),
            headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchedImage:
        timeout = self._settings.fetch_timeout_s
        try:
            return await asyncio.wait_for(self._fetch(url), timeout=timeout)
        except (TimeoutError, httpx.TimeoutException):
            raise ApiError(
                ErrorCode.UPSTREAM_TIMEOUT, f"Fetching the source image took longer than {timeout:g}s."
            ) from None
        except httpx.HTTPError as exc:
            raise ApiError(
                ErrorCode.UPSTREAM_UNREACHABLE, f"Could not fetch the source image: {type(exc).__name__}."
            ) from None

    async def _fetch(self, url: str) -> FetchedImage:
        current = url
        for hop in range(self._settings.max_redirects + 1):
            request = await self._pinned_request(current, via_redirect=hop > 0)
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
                data = await self._read_limited(response)
                return FetchedImage(data, response.headers.get("content-type"), current)
            finally:
                await response.aclose()
        raise ApiError(
            ErrorCode.TOO_MANY_REDIRECTS, f"Source redirected more than {self._settings.max_redirects} times."
        )

    async def _pinned_request(self, url: str, *, via_redirect: bool) -> httpx.Request:
        if problem := url_problem(url, self._settings):
            if via_redirect:
                raise ApiError(ErrorCode.URL_NOT_ALLOWED, f"Source redirected to a disallowed URL: {problem}")
            raise ApiError(ErrorCode.INVALID_URL, problem, param="url")

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname or ""
        port = parts.port or DEFAULT_PORTS[scheme]
        ip = await self._resolve_checked(host, port, via_redirect=via_redirect)

        ip_host = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
        netloc = ip_host if parts.port is None else f"{ip_host}:{port}"
        named_host = f"[{host}]" if ":" in host else host  # IPv6 literals keep their brackets in Host
        host_header = named_host if parts.port is None else f"{named_host}:{port}"
        pinned_url = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
        extensions = {"sni_hostname": host} if scheme == "https" else {}
        return self._client.build_request(
            "GET", pinned_url, headers={"Host": host_header}, extensions=extensions
        )

    async def _resolve_checked(self, host: str, port: int, *, via_redirect: bool) -> IPAddress:
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                addresses = [ipaddress.ip_address(a) for a in await self._resolver(host, port)]
            except (OSError, ValueError) as exc:
                log.debug("dns lookup failed host=%s error=%r", host, exc)
                addresses = []
            if not addresses:
                raise ApiError(ErrorCode.DNS_ERROR, f"Could not resolve host '{host}'.") from None

        # Refuse if *any* address is internal, rather than picking a public one:
        # a host that resolves to both is suspicious.
        if not all(is_public_ip(a) for a in addresses):
            where = "Source redirected to" if via_redirect else "URL points to"
            raise ApiError(
                ErrorCode.URL_NOT_ALLOWED,
                f"{where} a private, loopback, or otherwise non-public address ({host}).",
                param=None if via_redirect else "url",
            )
        return addresses[0]

    async def _read_limited(self, response: httpx.Response) -> bytes:
        limit = self._settings.max_source_bytes
        too_large = ApiError(
            ErrorCode.SOURCE_TOO_LARGE, f"Source image is larger than the {_human_bytes(limit)} limit."
        )
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise too_large
        buf = bytearray()
        # aiter_bytes() yields *decoded* bytes, so a gzip bomb is capped too.
        async for chunk in response.aiter_bytes():
            buf += chunk
            if len(buf) > limit:
                raise too_large
        return bytes(buf)


def _human_bytes(n: int) -> str:
    return f"{n / BYTES_PER_MB:g} MB" if n >= BYTES_PER_MB else f"{n:,} bytes"
