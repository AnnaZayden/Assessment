from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.fetcher import Fetcher
from app.main import create_app

# Hostnames used in tests and the address each "resolves" to.
DNS = {
    "img.test": "93.184.216.34",
    "other.test": "93.184.216.35",
    "internal.test": "10.0.0.5",
    "rebind.test": "93.184.216.36",  # overridden per test
}


@dataclass
class Upstream:
    """A fake internet. Routes are keyed by (Host header, path)."""

    routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    dns: dict[str, list[str]] = field(default_factory=lambda: {k: [v] for k, v in DNS.items()})

    def add(
        self, host: str, path: str, handler: Callable[[httpx.Request], httpx.Response] | httpx.Response
    ) -> None:
        self.routes[(host, path)] = handler if callable(handler) else (lambda _r, resp=handler: resp)

    def image(self, host: str, path: str, data: bytes, content_type: str = "image/jpeg") -> None:
        self.add(
            host, path, lambda _r: httpx.Response(200, content=data, headers={"content-type": content_type})
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.headers["host"], request.url.path))
        if handler is None:
            return httpx.Response(404)
        result = handler(request)
        if hasattr(result, "__await__"):
            result = await result
        return result

    async def resolve(self, host: str, port: int) -> list[str]:
        if host not in self.dns:
            raise OSError("NXDOMAIN")
        return self.dns[host]


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def make_client(upstream: Upstream) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(raise_server_exceptions: bool = True, **overrides: object) -> TestClient:
        s = Settings().with_overrides(**overrides)
        fetcher = Fetcher(s, resolver=upstream.resolve, transport=httpx.MockTransport(upstream.handle))
        client = TestClient(create_app(s, fetcher), raise_server_exceptions=raise_server_exceptions)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()
