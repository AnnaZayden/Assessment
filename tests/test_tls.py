"""The real network path: a local HTTPS server with a test CA, reached through the production HTTP client.

The fake transport used elsewhere cannot show that TLS works against a pinned IP, so these tests check
SNI, certificate verification, connection reuse and proxy settings against a real TLS stack.
"""

from __future__ import annotations

import asyncio
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import trustme

from app.config import Settings
from app.errors import ApiError, ErrorCode
from app.fetcher import Fetcher

from .helpers import make_image

BODY = make_image("PNG", size=(10, 10))


@dataclass
class Seen:
    connections: int = 0
    sni: list[str | None] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)


@dataclass
class TlsServer:
    port: int
    seen: Seen
    client_context: ssl.SSLContext


@pytest.fixture
def tls_server() -> Iterator[TlsServer]:
    ca = trustme.CA()
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("img.test", "other.test").configure_cert(server_context)
    seen = Seen()
    server_context.sni_callback = lambda _sock, name, _ctx: seen.sni.append(name)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            seen.hosts.append(self.headers["Host"])
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        def log_message(self, *_args: Any) -> None:
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self) -> tuple[Any, Any]:
            sock, addr = super().get_request()
            seen.connections += 1
            return server_context.wrap_socket(sock, server_side=True), addr

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_context = ssl.create_default_context()
    ca.configure_trust(client_context)
    yield TlsServer(server.server_address[1], seen, client_context)
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # The test server lives on 127.0.0.1, which the fetcher would (rightly) refuse.
    monkeypatch.setattr("app.fetcher.is_public_ip", lambda _ip: True)


async def _loopback(_host: str, _port: int) -> list[str]:
    return ["127.0.0.1"]


def fetch_all(server: TlsServer, *hosts: str) -> list[bytes]:
    async def run() -> list[bytes]:
        settings = Settings().with_overrides(allowed_ports=[server.port])
        fetcher = Fetcher(settings, resolver=_loopback, ssl_context=server.client_context)
        try:
            return [(await fetcher.fetch(f"https://{host}:{server.port}/a.png")).data for host in hosts]
        finally:
            await fetcher.aclose()

    return asyncio.run(run())


def test_pinned_ip_keeps_sni_host_header_and_verified_certificate(tls_server):
    assert fetch_all(tls_server, "img.test") == [BODY]
    assert tls_server.seen.sni == ["img.test"]  # TLS was for the name, though we dialled the IP
    assert tls_server.seen.hosts == [f"img.test:{tls_server.port}"]


def test_certificate_for_another_name_is_rejected(tls_server):
    with pytest.raises(ApiError) as exc:
        fetch_all(tls_server, "evil.test")
    assert exc.value.code is ErrorCode.UPSTREAM_UNREACHABLE
    assert tls_server.seen.hosts == []  # no HTTP request was sent over the unverified connection


def test_connections_are_never_reused(tls_server):
    fetch_all(tls_server, "img.test", "other.test", "img.test")
    assert tls_server.seen.connections == 3
    assert tls_server.seen.sni == ["img.test", "other.test", "img.test"]


def test_proxy_environment_is_ignored(tls_server, monkeypatch):
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # would fail if used
    assert fetch_all(tls_server, "img.test") == [BODY]
