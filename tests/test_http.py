"""The HTTP contract: parameters, errors, caching headers, rate limiting and service endpoints."""

from __future__ import annotations

import logging
import re

import pytest
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.observability import configure_logging

from .helpers import error_of, make_image, open_image

# --- Parameter validation ---------------------------------------------------------


def test_parameter_errors(client):
    base = "/process?url=https://img.test/a.jpg"
    cases = {
        "/process": ("missing_parameter", "url"),
        f"{base}&width=0": ("invalid_parameter", "width"),
        f"{base}&width=5000": ("invalid_parameter", "width"),
        f"{base}&width=abc": ("invalid_parameter", "width"),
        f"{base}&height=-1": ("invalid_parameter", "height"),
        f"{base}&quality=101": ("invalid_parameter", "quality"),
        f"{base}&format=bmp": ("invalid_parameter", "format"),
        f"{base}&crop=zoom": ("invalid_parameter", "crop"),
        f"{base}&width=10&crop=fill": ("invalid_parameter", "crop"),
        f"{base}&widht=10": ("unknown_parameter", "widht"),
        f"{base}&width=1&width=2": ("duplicate_parameter", "width"),
        "/process?url=": ("missing_parameter", "url"),
        f"{base}&width=-1": ("invalid_parameter", "width"),
        f"{base}&width=1.5": ("invalid_parameter", "width"),
        f"{base}&width=%2B5": ("invalid_parameter", "width"),
        f"{base}&width=%D9%A5": ("invalid_parameter", "width"),  # Arabic-Indic digit five
        f"{base}&height=0": ("invalid_parameter", "height"),
        f"{base}&height=5000": ("invalid_parameter", "height"),
        f"{base}&quality=0": ("invalid_parameter", "quality"),
        f"{base}&quality=abc": ("invalid_parameter", "quality"),
    }
    for path, (code, param) in cases.items():
        r = client.get(path)
        assert r.status_code == 400, path
        err = error_of(r)
        assert (err["code"], err["param"]) == (code, param), path
        assert err["message"]


def test_parameter_error_messages_say_what_to_fix(client):
    base = "/process?url=https://img.test/a.jpg"
    messages = {
        "format=bmp": "Unsupported format 'bmp'. Supported: jpeg, png, webp, gif, avif, auto.",
        "width=abc": "'width' must be an integer between 1 and 4000, got 'abc'.",
        "crop=fill&width=5": "crop=fill needs both 'width' and 'height'.",
        "widht=5": (
            "Unknown query parameter 'widht'. Allowed parameters: url, width, height, format, quality, crop."
        ),
        "width=%7Bx%7D": "'width' must be an integer between 1 and 4000, got '{x}'.",  # braces stay literal
    }
    for query, message in messages.items():
        assert error_of(client.get(f"{base}&{query}"))["message"] == message, query


# --- Caching -------------------------------------------------------------------------


def test_result_cache_and_conditional_requests(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image())
    params = {"url": "https://img.test/a.jpg", "width": 100}
    first = client.get("/process", params=params)
    assert first.headers["x-cache"] == "MISS"
    assert first.headers["cache-control"] == "public, max-age=86400"
    second = client.get("/process", params=params)
    assert second.headers["x-cache"] == "HIT"
    assert second.content == first.content
    assert len(upstream.requests) == 1

    not_modified = client.get("/process", params=params, headers={"If-None-Match": first.headers["etag"]})
    assert not_modified.status_code == 304
    assert not_modified.content == b""


def test_errors_are_not_cached(client, upstream):
    params = {"url": "https://img.test/later.jpg"}
    assert client.get("/process", params=params).status_code == 502
    upstream.image("img.test", "/later.jpg", make_image())
    assert client.get("/process", params=params).status_code == 200


# --- Caching ---------------------------------------------------------------------------------


def test_cache_key_distinguishes_parameters(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image())
    a = client.get("/process", params={"url": "https://img.test/a.jpg", "width": 100})
    b = client.get("/process", params={"url": "https://img.test/a.jpg", "width": 200})
    assert (a.headers["x-cache"], b.headers["x-cache"]) == ("MISS", "MISS")
    assert open_image(b.content).width == 200
    assert len(upstream.requests) == 2


def test_default_quality_shares_a_cache_entry(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image())
    first = client.get("/process", params={"url": "https://img.test/a.jpg"})
    second = client.get("/process", params={"url": "https://img.test/a.jpg", "quality": 80})
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")


def test_if_none_match_variants(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image())
    params = {"url": "https://img.test/a.jpg", "width": 10}
    etag = client.get("/process", params=params).headers["etag"]
    for header in ("*", f"W/{etag}", f'"other", {etag}'):
        r = client.get("/process", params=params, headers={"If-None-Match": header})
        assert r.status_code == 304, header
        assert r.headers["etag"] == etag
        assert r.headers["cache-control"] == "public, max-age=86400"
    assert client.get("/process", params=params, headers={"If-None-Match": '"other"'}).status_code == 200


# --- Rate limiting & misc -------------------------------------------------------------


def test_rate_limit(make_client, upstream):
    client = make_client(rate_limit_burst=3, rate_limit_per_minute=1)
    upstream.image("img.test", "/a.jpg", make_image())
    statuses = [
        client.get("/process", params={"url": "https://img.test/a.jpg"}).status_code for _ in range(4)
    ]
    assert statuses == [200, 200, 200, 429]
    r = client.get("/process", params={"url": "https://img.test/a.jpg"})
    assert error_of(r)["code"] == "rate_limited"
    assert int(r.headers["retry-after"]) > 0


def test_unknown_route_uses_error_format(client):
    r = client.get("/nope")
    assert r.status_code == 404
    assert error_of(r)["code"] == "not_found"


def test_video_thumbnail_not_implemented(client):
    r = client.get("/video/thumbnail", params={"url": "https://img.test/v.mp4"})
    assert r.status_code == 501
    assert error_of(r)["code"] == "not_implemented"


def test_limits_endpoint(client):
    body = client.get("/limits").json()
    assert body["limits"]["max_output_dimension"] == 4000
    assert "jpeg" in body["output_formats"]


# --- HTTP layer ------------------------------------------------------------------------------


def test_post_is_405_in_error_format(client):
    r = client.post("/process")
    assert r.status_code == 405
    assert error_of(r)["code"] == "method_not_allowed"


def test_unexpected_error_is_generic_500(make_client, upstream, monkeypatch):
    def explode(*_args):
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr("app.service.process_image", explode)
    client = make_client(raise_server_exceptions=False)
    upstream.image("img.test", "/a.jpg", make_image())
    r = client.get("/process", params={"url": "https://img.test/a.jpg"})
    assert r.status_code == 500
    assert error_of(r)["code"] == "internal_error"
    assert "secret" not in r.text


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Image Processing Service" in r.text


def test_openapi_documents_process_parameters(client):
    spec = client.get("/openapi.json").json()
    names = [p["name"] for p in spec["paths"]["/process"]["get"]["parameters"]]
    assert names == ["url", "width", "height", "format", "quality", "crop"]


def test_access_log_records_route_and_host_only(client, upstream, caplog):
    caplog.set_level(logging.INFO, logger="image_service")
    upstream.image("img.test", "/private/path.jpg", make_image())
    client.get("/process", params={"url": "https://img.test/private/path.jpg?token=hunter2"})
    client.get("/process", params={"url": "https://img.test/missing?token=hunter2"})
    records = [r for r in caplog.records if r.name == "image_service" and r.getMessage() == "request"]
    fields = [r.fields for r in records]
    assert [(f["route"], f["status"], f["source_host"]) for f in fields] == [
        ("/process", 200, "img.test"),
        ("/process", 502, "img.test"),
    ]
    assert fields[0]["cache"] == "MISS"
    assert fields[1]["code"] == "upstream_error"
    assert "hunter2" not in repr(fields)
    assert "private" not in repr(fields)


# --- Rate limiting ---------------------------------------------------------------------------


def test_rate_limit_remaining_header_counts_down(make_client, upstream):
    client = make_client(rate_limit_burst=3)
    upstream.image("img.test", "/a.jpg", make_image())
    remaining = [
        client.get("/process", params={"url": "https://img.test/a.jpg"}).headers["x-ratelimit-remaining"]
        for _ in range(3)
    ]
    assert remaining == ["2", "1", "0"]


def test_forwarded_for_is_ignored_by_default(make_client, upstream):
    client = make_client(rate_limit_burst=1, rate_limit_per_minute=1)
    upstream.image("img.test", "/a.jpg", make_image())
    statuses = [
        client.get(
            "/process", params={"url": "https://img.test/a.jpg"}, headers={"X-Forwarded-For": ip}
        ).status_code
        for ip in ("1.1.1.1", "2.2.2.2")
    ]
    assert statuses == [200, 429]


def test_forwarded_for_uses_rightmost_entry_when_trusted(make_client, upstream):
    client = make_client(rate_limit_burst=1, rate_limit_per_minute=1, trust_proxy_headers=True)
    upstream.image("img.test", "/a.jpg", make_image())

    def status(forwarded):
        return client.get(
            "/process", params={"url": "https://img.test/a.jpg"}, headers={"X-Forwarded-For": forwarded}
        ).status_code

    assert status("1.1.1.1, 9.9.9.9") == 200
    assert status("2.2.2.2, 9.9.9.9") == 429  # spoofed left-most entry doesn't help
    assert status("1.1.1.1, 8.8.8.8") == 200  # a different real client does


# --- Content negotiation (format=auto) -------------------------------------------------------


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        ("image/avif,image/webp,*/*", "image/avif"),
        ("image/webp,*/*;q=0.8", "image/webp"),
        ("image/avif;q=0, image/webp", "image/webp"),  # q=0 means "not acceptable"
        ("image/avif;q=oops, image/webp", "image/webp"),  # an unparsable q counts as 0
        ("*/*", "image/png"),  # nothing negotiable accepted: keep the source format
        (None, "image/png"),
    ],
)
def test_format_auto_negotiates_from_accept(client, upstream, accept, expected):
    upstream.image("img.test", "/a.png", make_image("PNG", size=(40, 20)))
    headers = {"Accept": accept} if accept else {}
    r = client.get("/process", params={"url": "https://img.test/a.png", "format": "auto"}, headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == expected
    assert r.headers["vary"] == "Accept"


def test_explicit_format_does_not_vary(client, upstream):
    upstream.image("img.test", "/a.png", make_image("PNG"))
    r = client.get(
        "/process", params={"url": "https://img.test/a.png", "format": "webp"}, headers={"Accept": "*/*"}
    )
    assert "vary" not in r.headers


def test_negotiated_formats_are_cached_separately(client, upstream):
    upstream.image("img.test", "/a.png", make_image("PNG"))
    params = {"url": "https://img.test/a.png", "format": "auto"}
    webp = client.get("/process", params=params, headers={"Accept": "image/webp"})
    avif = client.get("/process", params=params, headers={"Accept": "image/avif"})
    assert (webp.headers["content-type"], avif.headers["content-type"]) == ("image/webp", "image/avif")
    assert avif.headers["x-cache"] == "MISS"


# --- Observability ---------------------------------------------------------------------------


def test_request_id_is_generated_and_echoed(client):
    generated = client.get("/healthz").headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    assert client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    unsafe = client.get("/healthz", headers={"X-Request-ID": "bad id\r\nx"}).headers["x-request-id"]
    assert unsafe != "bad id\r\nx"
    assert re.fullmatch(r"[0-9a-f]{32}", unsafe)


def test_error_responses_carry_request_id(client):
    r = client.get("/process", headers={"X-Request-ID": "trace-1"})
    assert r.status_code == 400
    assert r.headers["x-request-id"] == "trace-1"


def test_metrics_endpoint(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image())
    for _ in range(2):
        client.get("/process", params={"url": "https://img.test/a.jpg", "width": 10})
    client.get("/process", params={"url": "ftp://x/"})
    client.get("/nope")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = r.text
    assert 'http_requests_total{route="/process",method="GET",status="200"} 2' in body
    assert 'http_requests_total{route="/process",method="GET",status="400"} 1' in body
    assert 'http_requests_total{route="unmatched",method="GET",status="404"} 1' in body
    assert 'image_cache_total{status="HIT"} 1' in body
    assert 'image_cache_total{status="MISS"} 1' in body
    assert 'image_errors_total{code="invalid_url"} 1' in body
    assert 'http_request_duration_seconds_count{route="/process"} 3' in body
    assert 'http_request_duration_seconds_bucket{route="/process",le="+Inf"} 3' in body
    assert "image_renders_in_flight 0" in body
    assert "rate_limit_tracked_clients 1" in body


def test_unexpected_framework_error_is_internal(make_client):
    client = make_client()

    @client.app.get("/teapot")
    async def teapot() -> None:
        raise StarletteHTTPException(418)

    r = client.get("/teapot")
    assert r.status_code == 500
    assert error_of(r)["code"] == "internal_error"


def test_full_source_urls_never_reach_the_logs(client, upstream):
    """httpx logs every request URL at INFO; configure_logging must keep that out of production logs."""
    seen: list[str] = []
    capture = logging.Handler()
    capture.emit = lambda record: seen.append(f"{record.name}: {record.getMessage()}")
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging(logging.INFO)
        root.addHandler(capture)
        upstream.image("img.test", "/private/path.jpg", make_image())
        client.get("/process", params={"url": "https://img.test/private/path.jpg?token=hunter2"})
    finally:
        root.handlers[:], root.level = saved
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.NOTSET)
    assert any(line.startswith("image_service: request") for line in seen)  # logging did happen
    assert not [line for line in seen if "hunter2" in line or "private" in line]
