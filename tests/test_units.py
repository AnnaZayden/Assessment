"""Building blocks in isolation: address checks, cache, rate limiter, DNS, formats, processor and params."""

from __future__ import annotations

import asyncio
import importlib
import ipaddress
from dataclasses import dataclass

import pytest
from PIL import Image, features

from app import formats
from app.cache import LRUCache
from app.config import Settings
from app.errors import ApiError, ErrorCode
from app.fetcher import Fetcher, system_resolver
from app.params import ProcessQuery, accepted_media_types, openapi_parameters
from app.processor import _color_family, _normalize_mode
from app.ratelimit import RateLimiter
from app.urls import is_public_ip


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "255.255.255.255",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "2002:7f00:1::",  # 6to4 of 127.0.0.1
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
    ],
)
def test_non_public_addresses(address):
    assert not is_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses(address):
    assert is_public_ip(ipaddress.ip_address(address))


@dataclass
class Blob:
    size: int


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_lru_evicts_by_bytes():
    cache: LRUCache[Blob] = LRUCache(max_bytes=10, ttl_s=60)
    cache.set("a", Blob(4))
    cache.set("b", Blob(4))
    cache.get("a")  # a is now most recent
    cache.set("c", Blob(4))
    assert cache.get("b") is None
    assert cache.get("a")
    assert cache.get("c")


def test_lru_expires():
    clock = FakeClock()
    cache: LRUCache[Blob] = LRUCache(max_bytes=10, ttl_s=5, clock=clock)
    cache.set("a", Blob(1))
    clock.now = 6
    assert cache.get("a") is None
    assert len(cache) == 0


def test_lru_skips_oversized_entries():
    cache: LRUCache[Blob] = LRUCache(max_bytes=10, ttl_s=60)
    cache.set("big", Blob(11))
    assert len(cache) == 0


def test_rate_limiter_refills():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, burst=2, clock=clock)
    assert limiter.check("ip").allowed
    assert limiter.check("ip").allowed
    denied = limiter.check("ip")
    assert not denied.allowed
    assert denied.retry_after_s == 1
    assert limiter.check("other-ip").allowed
    clock.now = 1.0
    assert limiter.check("ip").allowed


def test_rate_limiter_sweeps_idle_clients():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, burst=2, clock=clock, sweep_every=3)
    limiter.check("a")
    clock.now = 10
    limiter.check("b")
    limiter.check("b")  # third call triggers a sweep
    assert limiter.tracked_clients == 1  # only the recently active client is kept


def test_lru_replaces_existing_key_and_recounts_bytes():
    cache: LRUCache[Blob] = LRUCache(max_bytes=10, ttl_s=60)
    cache.set("a", Blob(8))
    cache.set("a", Blob(2))
    cache.set("b", Blob(8))  # fits only if the old 8-byte "a" was un-counted
    assert len(cache) == 2


# --- Real DNS resolution (numeric hosts; no network needed) ---------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "2130706433", "0x7f000001", "017700000001", "127.1"])
def test_alternate_loopback_spellings_resolve_to_loopback(host):
    addresses = asyncio.run(system_resolver(host, 80))
    assert addresses == ["127.0.0.1"]
    assert not is_public_ip(ipaddress.ip_address(addresses[0]))


def test_fetcher_refuses_alternate_loopback_spellings_end_to_end():
    async def run():
        fetcher = Fetcher(Settings())  # real resolver, real transport; refused before any connection
        try:
            for host in ("2130706433", "0x7f000001", "127.1", "localhost"):
                with pytest.raises(ApiError) as exc:
                    await fetcher.fetch(f"http://{host}/")
                assert exc.value.code is ErrorCode.URL_NOT_ALLOWED, host
        finally:
            await fetcher.aclose()

    asyncio.run(run())


def test_fetcher_rejects_bad_initial_url_directly():
    async def run():
        fetcher = Fetcher(Settings())
        try:
            with pytest.raises(ApiError) as exc:
                await fetcher.fetch("ftp://img.test/a.png")
            assert (exc.value.status, exc.value.code) == (400, ErrorCode.INVALID_URL)
        finally:
            await fetcher.aclose()

    asyncio.run(run())


# --- Formats ----------------------------------------------------------------------------


def test_formats_without_avif(monkeypatch):
    monkeypatch.setattr(features, "check", lambda _name: False)
    try:
        importlib.reload(formats)
        assert "AVIF" not in formats.INPUT_FORMATS
        assert "avif" not in formats.OUTPUT_FORMATS
    finally:
        monkeypatch.undo()
        importlib.reload(formats)
    assert "avif" in formats.OUTPUT_FORMATS


# --- Processor helpers ------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "expected"), [("I", "L"), ("F", "L"), ("YCbCr", "RGB"), ("RGBA", "RGBA")])
def test_normalize_mode(mode, expected):
    assert _normalize_mode(Image.new(mode, (4, 4))).mode == expected


@pytest.mark.parametrize(
    ("mode", "family"), [("I;16", "gray"), ("F", "gray"), ("P", "rgb"), ("CMYK", "other")]
)
def test_color_family(mode, family):
    assert _color_family(mode) == family


# --- Params internals ---------------------------------------------------------------------


def test_query_model_requires_settings_context():
    with pytest.raises(TypeError, match="context"):
        ProcessQuery.model_validate({"url": "https://a.test/x"})


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("image/avif,image/webp;q=0.9", {"image/avif", "image/webp"}),
        ("IMAGE/WEBP ; Q=0", set()),
        ("image/webp;level=1;q=0.5", {"image/webp"}),  # other parameters are ignored
        (",,", set()),
        (None, set()),
    ],
)
def test_accepted_media_types(header, expected):
    assert accepted_media_types(header) == expected


def test_openapi_parameters_come_from_the_schema():
    params = {p["name"]: p for p in openapi_parameters()}
    assert list(params) == ["url", "width", "height", "format", "quality", "crop"]
    assert params["url"]["required"] is True
    assert params["width"]["required"] is False
    assert "auto" in str(params["format"]["schema"])
    assert "fill" in str(params["crop"]["schema"])
