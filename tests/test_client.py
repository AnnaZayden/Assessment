"""The typed client SDK, run against the real app (through the fake upstream) and against odd responses."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from image_client import ImageClient, ImageServiceError

from .helpers import make_image, open_image

SOURCE = "https://img.test/a.jpg"


@pytest.fixture
def images(client: TestClient) -> ImageClient:
    return ImageClient("http://testserver/", http=client)


def test_url_builds_the_query(images):
    url = images.url(SOURCE, width=400, format="webp", crop="fit")
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("http", "testserver", "/process")
    assert parse_qs(parts.query) == {"url": [SOURCE], "width": ["400"], "format": ["webp"], "crop": ["fit"]}


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"width": 0}, "width must be at least 1"),
        ({"quality": 101}, "quality must be between 1 and 100"),
        ({"width": 10, "crop": "fill"}, "needs both width and height"),
    ],
)
def test_url_rejects_obvious_mistakes_locally(images, options, message):
    with pytest.raises(ValueError, match=message):
        images.url(SOURCE, **options)


def test_process_returns_typed_result(images, upstream):
    upstream.image("img.test", "/a.jpg", make_image(size=(800, 600)))
    result = images.process(SOURCE, width=400, format="png")
    assert (result.width, result.height, result.content_type) == (400, 300, "image/png")
    assert open_image(result.data).size == (400, 300)
    assert (result.source.format, result.source.width, result.source.height) == ("jpeg", 800, 600)
    assert result.source.bytes > 0
    assert result.cache == "MISS"
    assert result.etag.startswith('"')
    assert len(result.request_id) == 32


def test_process_negotiates_with_accept(images, upstream):
    upstream.image("img.test", "/a.jpg", make_image())
    assert images.process(SOURCE, format="auto", accept="image/webp").content_type == "image/webp"


def test_api_errors_become_typed_exceptions(images):
    with pytest.raises(ImageServiceError) as exc:
        images.process(SOURCE)  # the fake upstream has no such image: 404 upstream
    err = exc.value
    assert (err.status, err.code, err.upstream_status) == (502, "upstream_error", 404)
    assert err.retryable
    assert err.request_id


def test_validation_errors_name_the_parameter(images):
    with pytest.raises(ImageServiceError) as exc:
        images.process("ftp://img.test/a.jpg")
    assert (exc.value.status, exc.value.code, exc.value.param) == (400, "invalid_url", "url")
    assert not exc.value.retryable


def test_rate_limit_error_carries_retry_after(make_client, upstream):
    upstream.image("img.test", "/a.jpg", make_image())
    images = ImageClient("http://testserver", http=make_client(rate_limit_burst=1, rate_limit_per_minute=1))
    images.process(SOURCE)
    with pytest.raises(ImageServiceError) as exc:
        images.process(SOURCE)
    assert (exc.value.code, exc.value.status) == ("rate_limited", 429)
    assert exc.value.retry_after_s
    assert exc.value.retry_after_s > 0
    assert exc.value.retryable


def test_limits(images):
    assert "max_output_dimension" in images.limits()["limits"]


def _client_for(handler: Callable[[httpx.Request], httpx.Response]) -> ImageClient:
    return ImageClient("http://svc", http=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="<html>Bad gateway page from a proxy</html>"),
        httpx.Response(500, json={"unexpected": "shape"}),
        httpx.Response(502, json={"error": None}),
    ],
    ids=["html", "wrong-json", "null-error"],
)
def test_non_api_error_bodies(response):
    with pytest.raises(ImageServiceError) as exc:
        _client_for(lambda _r: response).process(SOURCE)
    assert (exc.value.code, exc.value.status) == ("unexpected_response", response.status_code)
    assert exc.value.param is None
    assert exc.value.retry_after_s is None


def test_network_failure_is_retryable():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ImageServiceError) as exc:
        _client_for(refuse).process(SOURCE)
    assert (exc.value.code, exc.value.status) == ("network_error", None)
    assert exc.value.retryable


def test_owned_http_client_is_closed(monkeypatch):
    created: list[httpx.Client] = []
    real_client = httpx.Client

    def make_client(**kwargs):
        created.append(real_client(**kwargs))
        return created[-1]

    monkeypatch.setattr("image_client.httpx.Client", make_client)
    with ImageClient("http://svc", timeout_s=5):
        pass
    assert len(created) == 1
    assert created[0].is_closed
    assert created[0].timeout.read == 5


def test_shared_http_client_is_left_open():
    shared = httpx.Client()
    with ImageClient("http://svc", http=shared):
        pass
    assert not shared.is_closed
    shared.close()
