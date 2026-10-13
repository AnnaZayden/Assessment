"""Fetching untrusted URLs: address checks, redirects, timeouts and size limits."""

from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

from .helpers import error_of, make_image, open_image

# --- SSRF / untrusted input ---------------------------------------------------


def test_connects_to_pinned_ip_with_original_host(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image())
    client.get("/process", params={"url": "https://img.test/a.jpg"})
    sent = upstream.requests[0]
    assert sent.url.host == "93.184.216.34"
    assert sent.headers["host"] == "img.test"
    assert sent.extensions["sni_hostname"] == "img.test"


def test_rejects_private_ip_literal(client, upstream):
    for url in ["http://127.0.0.1/x.png", "http://169.254.169.254/latest/meta-data", "http://[::1]/x"]:
        r = client.get("/process", params={"url": url})
        assert r.status_code == 403, url
        assert error_of(r)["code"] == "url_not_allowed"
    assert upstream.requests == []


def test_rejects_host_resolving_to_private(client, upstream):
    r = client.get("/process", params={"url": "https://internal.test/x.png"})
    assert r.status_code == 403
    assert upstream.requests == []


def test_rejects_host_with_any_private_address(client, upstream):
    upstream.dns["rebind.test"] = ["93.184.216.36", "127.0.0.1"]
    r = client.get("/process", params={"url": "https://rebind.test/x.png"})
    assert r.status_code == 403
    assert error_of(r)["code"] == "url_not_allowed"
    assert upstream.requests == []


def test_rejects_redirect_to_private(client, upstream):
    upstream.add("img.test", "/r", httpx.Response(302, headers={"location": "http://internal.test/secret"}))
    r = client.get("/process", params={"url": "https://img.test/r"})
    assert r.status_code == 403
    assert "redirected" in error_of(r)["message"]
    assert len(upstream.requests) == 1


def test_rejects_redirect_to_other_scheme(client, upstream):
    upstream.add("img.test", "/r", httpx.Response(302, headers={"location": "file:///etc/passwd"}))
    r = client.get("/process", params={"url": "https://img.test/r"})
    assert r.status_code == 403
    assert error_of(r)["code"] == "url_not_allowed"
    assert len(upstream.requests) == 1


def test_follows_public_redirect(client, upstream):
    upstream.add("img.test", "/r", httpx.Response(301, headers={"location": "https://other.test/a.jpg"}))
    upstream.image("other.test", "/a.jpg", make_image())
    r = client.get("/process", params={"url": "https://img.test/r", "width": 10})
    assert r.status_code == 200
    assert open_image(r.content).width == 10
    assert [req.headers["host"] for req in upstream.requests] == ["img.test", "other.test"]


def test_too_many_redirects(client, upstream):
    upstream.add("img.test", "/loop", httpx.Response(302, headers={"location": "/loop"}))
    r = client.get("/process", params={"url": "https://img.test/loop"})
    assert r.status_code == 502
    assert error_of(r)["code"] == "too_many_redirects"
    assert len(upstream.requests) == 4  # initial + 3 redirects


def test_url_validation(client):
    cases = {
        "ftp://img.test/a.png": "invalid_url",
        "https://user:pw@img.test/a.png": "invalid_url",
        "https://img.test:8080/a.png": "invalid_url",
        "not a url": "invalid_url",
    }
    for url, code in cases.items():
        r = client.get("/process", params={"url": url})
        assert r.status_code == 400, url
        assert error_of(r) == {**error_of(r), "code": code, "param": "url"}


def test_dns_failure(client):
    r = client.get("/process", params={"url": "https://nope.test/a.png"})
    assert r.status_code == 502
    assert error_of(r)["code"] == "dns_error"


# --- Resource limits -------------------------------------------------------------


def test_source_too_large_by_content_length(make_client, upstream):
    client = make_client(max_source_bytes=1000)
    upstream.image("img.test", "/big.jpg", make_image(size=(2000, 2000)))
    r = client.get("/process", params={"url": "https://img.test/big.jpg"})
    assert r.status_code == 413
    assert error_of(r)["code"] == "source_too_large"


def test_declared_oversize_is_refused_before_reading_the_body(make_client, upstream):
    client = make_client(max_source_bytes=1000)
    sent = []

    async def chunks():
        for _ in range(100):
            sent.append(1)
            yield b"x" * 100

    upstream.add(
        "img.test",
        "/big",
        lambda _r: httpx.Response(200, headers={"content-length": "10000"}, content=chunks()),
    )
    r = client.get("/process", params={"url": "https://img.test/big"})
    assert r.status_code == 413
    assert sent == []  # refused on the header alone


def test_source_too_large_when_streamed_without_length(make_client, upstream):
    client = make_client(max_source_bytes=1000)

    async def chunks():
        for _ in range(100):
            yield b"x" * 100

    upstream.add("img.test", "/stream", lambda _r: httpx.Response(200, content=chunks()))
    r = client.get("/process", params={"url": "https://img.test/stream"})
    assert r.status_code == 413
    assert error_of(r)["code"] == "source_too_large"


def test_too_many_source_pixels(make_client, upstream):
    client = make_client(max_input_pixels=10_000)
    upstream.image("img.test", "/a.png", make_image("PNG", size=(200, 200)))
    r = client.get("/process", params={"url": "https://img.test/a.png"})
    assert r.status_code == 422
    assert error_of(r)["code"] == "source_too_many_pixels"


def test_derived_dimension_too_large(client, upstream):
    upstream.image("img.test", "/tall.png", make_image("PNG", size=(10, 1000)))
    r = client.get("/process", params={"url": "https://img.test/tall.png", "width": 100})
    assert r.status_code == 422
    assert error_of(r)["code"] == "output_too_large"


def test_upstream_timeout(make_client, upstream):
    client = make_client(fetch_timeout_s=0.2, connect_timeout_s=0.1)

    async def slow(_r):
        await asyncio.sleep(2)
        return httpx.Response(200)

    upstream.add("img.test", "/slow", slow)
    r = client.get("/process", params={"url": "https://img.test/slow"})
    assert r.status_code == 504
    assert error_of(r)["code"] == "upstream_timeout"


# --- More fetch failures and SSRF edges ---------------------------------------------------


def test_upstream_connection_refused(client, upstream):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    upstream.add("img.test", "/a.jpg", refuse)
    r = client.get("/process", params={"url": "https://img.test/a.jpg"})
    assert r.status_code == 502
    assert error_of(r)["code"] == "upstream_unreachable"


def test_redirect_without_location(client, upstream):
    upstream.add("img.test", "/r", httpx.Response(302))
    r = client.get("/process", params={"url": "https://img.test/r"})
    assert r.status_code == 502
    assert error_of(r)["code"] == "upstream_error"


def test_redirect_to_disallowed_port(client, upstream):
    upstream.add("img.test", "/r", httpx.Response(302, headers={"location": "https://other.test:8080/a.jpg"}))
    r = client.get("/process", params={"url": "https://img.test/r"})
    assert r.status_code == 403
    assert "Port 8080" in error_of(r)["message"]
    assert len(upstream.requests) == 1


def test_redirect_to_ip_literal_is_checked(client, upstream):
    upstream.add("img.test", "/r", httpx.Response(302, headers={"location": "http://[::ffff:127.0.0.1]/"}))
    r = client.get("/process", params={"url": "https://img.test/r"})
    assert r.status_code == 403
    assert error_of(r)["code"] == "url_not_allowed"
    assert len(upstream.requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:image/png;base64,AAAA",
        "gopher://img.test/",
        "javascript:alert(1)",
        "//img.test/a",
    ],
)
def test_initial_url_schemes_rejected(client, upstream, url):
    r = client.get("/process", params={"url": url})
    assert r.status_code == 400
    assert error_of(r)["code"] == "invalid_url"
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://img.test/" + "a" * 2100, "longer than 2048"),
        ("https://img.test:99999/a.jpg", "could not be parsed"),
        ("https:///a.jpg", "host name"),
    ],
)
def test_url_syntax_edges(client, url, message):
    r = client.get("/process", params={"url": url})
    assert r.status_code == 400
    assert message in error_of(r)["message"]


def test_uppercase_scheme_is_accepted(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image())
    assert client.get("/process", params={"url": "HTTPS://img.test/a.jpg", "width": 10}).status_code == 200


def test_ipv6_public_literal_is_pinned(client, upstream):
    upstream.image("[2606:4700::1]", "/a.jpg", make_image())
    r = client.get("/process", params={"url": "https://[2606:4700::1]/a.jpg", "width": 10})
    assert r.status_code == 200
    assert upstream.requests[0].url.host == "2606:4700::1"


def test_slow_body_hits_the_overall_deadline(make_client, upstream):
    client = make_client(fetch_timeout_s=0.3, connect_timeout_s=0.1)

    async def drip():
        for _ in range(20):
            await asyncio.sleep(0.05)
            yield b"x"

    upstream.add("img.test", "/drip", lambda _r: httpx.Response(200, content=drip()))
    r = client.get("/process", params={"url": "https://img.test/drip"})
    assert r.status_code == 504
    assert error_of(r)["code"] == "upstream_timeout"


def test_deadline_spans_redirects(make_client, upstream):
    client = make_client(fetch_timeout_s=0.3, connect_timeout_s=0.1)

    async def slow_redirect(request):
        await asyncio.sleep(0.2)
        return httpx.Response(302, headers={"location": f"{request.url.path}x"})

    for path in ("/r", "/rx", "/rxx"):
        upstream.add("img.test", path, slow_redirect)
    r = client.get("/process", params={"url": "https://img.test/r"})
    assert r.status_code == 504


def test_gzip_bomb_is_capped(make_client, upstream):
    client = make_client(max_source_bytes=1000)
    bomb = gzip.compress(b"\0" * 100_000)
    upstream.add("img.test", "/bomb", httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"}))
    r = client.get("/process", params={"url": "https://img.test/bomb"})
    assert r.status_code == 413
    assert error_of(r)["code"] == "source_too_large"


def test_lying_content_length_is_capped(make_client, upstream):
    client = make_client(max_source_bytes=1000)

    async def chunks():
        for _ in range(50):
            yield b"x" * 100

    upstream.add(
        "img.test", "/lie", lambda _r: httpx.Response(200, headers={"content-length": "10"}, content=chunks())
    )
    r = client.get("/process", params={"url": "https://img.test/lie"})
    assert r.status_code == 413


def test_size_limit_message_uses_megabytes(make_client, upstream):
    client = make_client(max_source_bytes=2 * 1024 * 1024)
    upstream.add("img.test", "/big", httpx.Response(200, headers={"content-length": str(10**8)}, content=b""))
    r = client.get("/process", params={"url": "https://img.test/big"})
    assert error_of(r)["message"] == "Source image is larger than the 2 MB limit."
