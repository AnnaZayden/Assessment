"""End-to-end tests for the web UI: a real browser against a real server, with the fake upstream behind it."""

from __future__ import annotations

import asyncio
import re
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from playwright.sync_api import Browser, Error, Page, expect, sync_playwright

from app.config import Settings
from app.fetcher import Fetcher
from app.main import create_app

from .conftest import Upstream
from .helpers import make_image

PHOTO = make_image(size=(800, 600))


@pytest.fixture(scope="module")
def upstream() -> Upstream:
    up = Upstream()
    up.image("img.test", "/photo.jpg", PHOTO)
    up.add(
        "img.test", "/page", httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})
    )

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)
        return httpx.Response(200, content=PHOTO)

    up.add("img.test", "/slow.jpg", slow)
    return up


@pytest.fixture(scope="module")
def server(upstream: Upstream) -> Iterator[str]:
    settings = Settings().with_overrides(rate_limit_burst=1000)
    fetcher = Fetcher(settings, resolver=upstream.resolve, transport=httpx.MockTransport(upstream.handle))
    uv = uvicorn.Server(uvicorn.Config(create_app(settings, fetcher), log_level="warning", lifespan="off"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    uv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Error:  # no Playwright-managed Chromium; fall back to an installed Chrome
            b = p.chromium.launch(channel="chrome")
        yield b
        b.close()


@pytest.fixture
def page(browser: Browser, server: str) -> Iterator[Page]:
    context = browser.new_context(base_url=server, permissions=["clipboard-read", "clipboard-write"])
    pg = context.new_page()
    # The original image is loaded straight from its source; serve the fake host's bytes to the browser too.
    pg.route("https://img.test/**", lambda route: route.fulfill(body=PHOTO, content_type="image/jpeg"))
    pg.goto("/")
    yield pg
    context.close()


def process(page: Page, url: str, **fields: str) -> None:
    page.fill("#url", url)
    for name, value in fields.items():
        if name in ("crop", "format"):
            page.select_option(f"#{name}", value)
        else:
            page.fill(f"#{name}", value)
    page.click("#submit")


def test_empty_state(page: Page):
    expect(page.locator("#original-stage")).to_contain_text("The source image appears here.")
    expect(page.locator("#stage")).to_contain_text("Pick a sample or paste an image URL")
    for dl in ("#original-meta", "#meta", "#summary"):
        expect(page.locator(dl)).to_be_hidden()


def test_controls_cover_every_api_parameter(page: Page, server: str):
    api_params = httpx.get(f"{server}/limits").json()["parameters"]
    for name in api_params:
        expect(page.locator(f"#{name}")).to_be_visible()
    formats = page.locator("#format option").all_inner_texts()
    assert set(httpx.get(f"{server}/limits").json()["output_formats"]) <= set(formats)


def test_success_shows_original_and_processed_side_by_side(page: Page):
    process(page, "https://img.test/photo.jpg", width="400", format="webp")
    original, processed = page.locator("#original-meta"), page.locator("#meta")
    expect(processed).to_be_visible()
    expect(original).to_contain_text("800 × 600")
    expect(original).to_contain_text("jpeg")
    expect(original).to_contain_text(f"{len(PHOTO) / 1024:.1f} KB")
    expect(processed).to_contain_text("400 × 300")
    expect(processed).to_contain_text("webp")
    expect(processed).to_contain_text(re.compile(r"Size\d+(\.\d+)? (B|KB|MB)"))
    expect(page.locator("#summary")).to_contain_text("200")
    expect(page.locator("#original-stage img")).to_be_visible()
    expect(page.locator("#stage img")).to_be_visible()
    boxes = [page.locator(sel).bounding_box() for sel in ("#original-stage", "#stage")]
    assert boxes[0]["x"] < boxes[1]["x"]
    assert abs(boxes[0]["y"] - boxes[1]["y"]) < 1


def test_loading_state(page: Page):
    process(page, "https://img.test/slow.jpg")
    expect(page.locator("#submit")).to_be_disabled()
    expect(page.locator("#submit")).to_have_text("Processing…")
    expect(page.locator("#stage .spinner")).to_be_visible()
    expect(page.locator("#original-stage .spinner")).to_be_visible()
    expect(page.locator("#meta")).to_be_visible(timeout=10_000)
    expect(page.locator("#submit")).to_be_enabled()


@pytest.mark.parametrize(
    ("url", "fields", "expected"),
    [
        ("https://img.test/photo.jpg", {"width": "10", "crop": "fill"}, "400 · invalid_parameter"),
        ("http://169.254.169.254/latest/meta-data/", {}, "403 · url_not_allowed"),
        ("https://img.test/page", {}, "415 · unsupported_media_type"),
        ("https://nowhere.test/a.jpg", {}, "502 · dns_error"),
    ],
    ids=["validation", "blocked", "unsupported", "unreachable"],
)
def test_api_errors_are_shown(page: Page, url, fields, expected):
    process(page, url, **fields)
    error = page.locator("#stage .error")
    expect(error).to_contain_text(expected)
    expect(error.locator("pre")).to_contain_text('"error"')
    expect(page.locator("#original-stage")).to_contain_text("No source image to show.")


def test_error_after_success_clears_stale_details(page: Page):
    process(page, "https://img.test/photo.jpg", width="100")
    expect(page.locator("#meta")).to_be_visible()
    process(page, "http://127.0.0.1/")
    expect(page.locator("#stage .error")).to_be_visible()
    expect(page.locator("#meta")).to_be_hidden()
    expect(page.locator("#original-meta")).to_be_hidden()
    expect(page.locator("#processed-scale")).to_be_hidden()
    expect(page.locator("#original-scale")).to_be_hidden()


def test_network_failure_is_shown(page: Page):
    page.route("**/process?**", lambda route: route.abort())
    process(page, "https://img.test/photo.jpg")
    expect(page.locator("#stage .error")).to_contain_text("network_error")
    expect(page.locator("#submit")).to_be_enabled()


def test_original_blocked_by_its_host(page: Page):
    page.route("https://img.test/**", lambda route: route.abort())
    process(page, "https://img.test/photo.jpg", width="100")
    expect(page.locator("#original-stage")).to_contain_text("doesn't allow the image to be shown here")
    expect(page.locator("#original-meta")).to_contain_text("800 × 600")


def test_request_url_and_curl_update_and_copy(page: Page, server: str):
    page.fill("#url", "https://img.test/photo.jpg")
    page.fill("#width", "300")
    page.select_option("#format", "png")
    path = "/process?url=https%3A%2F%2Fimg.test%2Fphoto.jpg&width=300&format=png"
    expect(page.locator("#request-url")).to_have_text(path)
    expect(page.locator("#request-curl")).to_have_text(f"curl -sS -D - -o out.img '{server}{path}'")

    page.click("[data-copy=url]")
    expect(page.locator("[data-copy=url]")).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()") == f"{server}{path}"
    page.click("[data-copy=curl]")
    assert page.evaluate("navigator.clipboard.readText()") == f"curl -sS -D - -o out.img '{server}{path}'"


def test_sample_buttons_fill_the_url(page: Page):
    page.click("text=Blocked: metadata IP")
    expect(page.locator("#url")).to_have_value("http://169.254.169.254/latest/meta-data/")
    expect(page.locator("#request-url")).to_contain_text("169.254.169.254")


def test_ui_only_uses_the_public_api(page: Page, server: str):
    seen: list[str] = []
    page.on("request", lambda request: seen.append(request.url))
    process(page, "https://img.test/photo.jpg", width="100")
    expect(page.locator("#meta")).to_be_visible()
    own = [url.removeprefix(server).split("?")[0] for url in seen if url.startswith(server)]
    assert own == ["/process"]


def test_previews_say_how_much_they_are_shrunk_and_link_to_full_size(page: Page):
    process(page, "https://img.test/photo.jpg", width="3000")
    expect(page.locator("#meta")).to_contain_text("3000 × 2250")
    for note, stage in (("#processed-scale", "#stage"), ("#original-scale", "#original-stage")):
        expect(page.locator(note)).to_be_visible()
        expect(page.locator(note)).to_have_text(
            re.compile(r"Shown at \d+% of actual size\. Click to open full size\.")
        )
        # The note follows layout changes (e.g. a scrollbar appearing); wait for it to settle.
        page.wait_for_function(
            """([note, stage]) => {
                const img = document.querySelector(stage + " img");
                const shown = Math.round((img.clientWidth / img.naturalWidth) * 100);
                return document.querySelector(note).textContent.startsWith(`Shown at ${shown}% `);
            }""",
            arg=[note, stage],
        )
        link = page.locator(f"{stage} a.zoom")
        assert link.get_attribute("target") == "_blank"
    assert page.locator("#stage a.zoom").get_attribute("href").startswith("blob:")
    assert page.locator("#original-stage a.zoom").get_attribute("href") == "https://img.test/photo.jpg"


def test_small_images_are_shown_at_actual_size(page: Page):
    process(page, "https://img.test/photo.jpg", width="100")
    expect(page.locator("#processed-scale")).to_be_visible()
    expect(page.locator("#processed-scale")).to_have_text(
        "Shown at actual size. Click to open it in a new tab."
    )


def test_size_change_is_signed(page: Page):
    process(page, "https://img.test/photo.jpg", width="100")
    expect(page.locator("#meta")).to_contain_text(re.compile(r"vs original\s*-\d+%", re.IGNORECASE))
    process(page, "https://img.test/photo.jpg", width="3000", format="png")
    expect(page.locator("#meta")).to_contain_text(
        re.compile(r"vs original\s*\+\d+% \(\d+\.\d× larger\)", re.IGNORECASE)
    )
