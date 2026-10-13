"""The image service: request coalescing, cancellation, concurrency limits and load shedding."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.config import Settings
from app.errors import ApiError, ErrorCode
from app.fetcher import FetchedImage
from app.params import ProcessParams
from app.processor import process_image
from app.service import ImageService

from .helpers import make_image

# --- Service: coalescing, cancellation, concurrency limits ------------------------------


class FakeFetcher:
    def __init__(self, delay: float = 0.05, fail: Exception | None = None) -> None:
        self.calls = 0
        self.active = 0
        self.peak = 0
        self.delay = delay
        self.fail = fail
        self.release = asyncio.Event()
        self.block = False

    async def fetch(self, url: str) -> FetchedImage:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.block:
                await self.release.wait()
            await asyncio.sleep(self.delay)
            if self.fail:
                raise self.fail
            return FetchedImage(make_image(size=(20, 20)), "image/jpeg", url)
        finally:
            self.active -= 1


def test_identical_concurrent_requests_coalesce():
    async def run():
        fetcher = FakeFetcher()
        service = ImageService(Settings(), fetcher)
        results = await asyncio.gather(
            *(service.get(ProcessParams(url="https://a.test/x", quality=80)) for _ in range(5))
        )
        statuses = sorted(status for _, status in results)
        assert statuses == ["COALESCED"] * 4 + ["MISS"]
        assert fetcher.calls == 1
        assert len({image.etag for image, _ in results}) == 1
        _, again = await service.get(ProcessParams(url="https://a.test/x", quality=80))
        assert again == "HIT"

    asyncio.run(run())


def test_coalesced_waiters_share_errors_and_nothing_is_cached():
    async def run():
        fetcher = FakeFetcher(fail=ApiError(ErrorCode.UPSTREAM_ERROR, "boom"))
        service = ImageService(Settings(), fetcher)
        params = ProcessParams(url="https://a.test/x", quality=80)
        results = await asyncio.gather(*(service.get(params) for _ in range(3)), return_exceptions=True)
        assert all(isinstance(r, ApiError) and r.code is ErrorCode.UPSTREAM_ERROR for r in results)
        assert fetcher.calls == 1
        fetcher.fail = None
        _, status = await service.get(params)
        assert status == "MISS"

    asyncio.run(run())


def test_cancelled_leader_cancels_waiters_and_clears_inflight():
    async def run():
        fetcher = FakeFetcher()
        fetcher.block = True
        service = ImageService(Settings(), fetcher)
        params = ProcessParams(url="https://a.test/x", quality=80)
        leader = asyncio.create_task(service.get(params))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(service.get(params))
        await asyncio.sleep(0)
        leader.cancel()
        for task in (leader, waiter):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert service.in_flight == 0
        fetcher.block = False
        _, status = await service.get(params)  # a fresh request works afterwards
        assert status == "MISS"

    asyncio.run(run())


def test_fetch_concurrency_is_limited():
    async def run():
        fetcher = FakeFetcher()
        service = ImageService(Settings().with_overrides(max_concurrent_fetches=2), fetcher)
        await asyncio.gather(
            *(service.get(ProcessParams(url=f"https://a.test/{i}", quality=80)) for i in range(6))
        )
        assert fetcher.calls == 6
        assert fetcher.peak == 2

    asyncio.run(run())


def test_decode_concurrency_is_limited(monkeypatch):
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def tracking_process(*args):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return process_image(*args)

    monkeypatch.setattr("app.service.process_image", tracking_process)

    async def run():
        service = ImageService(Settings().with_overrides(max_concurrent_decodes=2), FakeFetcher(delay=0))
        await asyncio.gather(
            *(service.get(ProcessParams(url=f"https://a.test/{i}", quality=80)) for i in range(6))
        )

    asyncio.run(run())
    assert state["peak"] == 2


# --- Load shedding ------------------------------------------------------------------------


def test_service_sheds_load_beyond_pending_limit():
    async def run():
        fetcher = FakeFetcher()
        fetcher.block = True
        service = ImageService(Settings().with_overrides(max_pending_renders=2), fetcher)
        running = [
            asyncio.create_task(service.get(ProcessParams(url=f"https://a.test/{i}", quality=80)))
            for i in range(2)
        ]
        await asyncio.sleep(0)
        with pytest.raises(ApiError) as exc:  # must be refused at once, not queued
            await asyncio.wait_for(
                service.get(ProcessParams(url="https://a.test/extra", quality=80)), timeout=1
            )
        assert exc.value.code is ErrorCode.SERVICE_BUSY
        assert exc.value.status == 503
        assert exc.value.headers["Retry-After"] == "1"
        # Requests identical to one already running still join it instead of being shed.
        joined = asyncio.create_task(service.get(ProcessParams(url="https://a.test/0", quality=80)))
        fetcher.block = False
        fetcher.release.set()
        statuses = [status for _, status in await asyncio.gather(*running, joined)]
        assert statuses == ["MISS", "MISS", "COALESCED"]

    asyncio.run(run())
