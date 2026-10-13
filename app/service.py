"""Fetch -> process -> cache, with request coalescing, concurrency limits, and load shedding."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from starlette.concurrency import run_in_threadpool

from .cache import LRUCache
from .config import Settings
from .errors import ApiError, ErrorCode
from .fetcher import Fetcher
from .formats import OutputFormat
from .params import ProcessParams
from .processor import SourceInfo, process_image
from .video import extract_frame

ETAG_HEX_CHARS: Final = 32  # 128 bits of SHA-256: collision-safe, and shorter headers
BUSY_RETRY_AFTER_S: Final = 1


class CacheStatus(StrEnum):
    HIT = "HIT"
    MISS = "MISS"
    COALESCED = "COALESCED"  # an identical request was already in progress; we shared its result


@dataclass(frozen=True)
class RenderedImage:
    data: bytes
    content_type: str
    format: OutputFormat
    width: int
    height: int
    etag: str
    source: SourceInfo

    @property
    def size(self) -> int:
        return len(self.data)


def _retrieve_exception(future: asyncio.Future[RenderedImage]) -> None:
    """Mark a failed future's exception as seen, so asyncio doesn't warn when no request awaited it."""
    if not future.cancelled():
        future.exception()


class ImageService:
    def __init__(self, settings: Settings, fetcher: Fetcher) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._cache: LRUCache[RenderedImage] = LRUCache(settings.cache_max_bytes, settings.cache_ttl_s)
        self._inflight: dict[str, asyncio.Future[RenderedImage]] = {}
        self._fetch_slots = asyncio.Semaphore(settings.max_concurrent_fetches)
        self._decode_slots = asyncio.Semaphore(settings.max_concurrent_decodes)

    @property
    def in_flight(self) -> int:
        """Distinct renders currently running or queued for a slot."""
        return len(self._inflight)

    async def get(self, params: ProcessParams) -> tuple[RenderedImage, CacheStatus]:
        key = params.cache_key()
        if (cached := self._cache.get(key)) is not None:
            return cached, CacheStatus.HIT

        # Identical requests already in progress share one fetch + render.
        if (pending := self._inflight.get(key)) is not None:
            return await asyncio.shield(pending), CacheStatus.COALESCED

        if len(self._inflight) >= self._settings.max_pending_renders:
            raise ApiError(
                ErrorCode.SERVICE_BUSY,
                "The service is at capacity. Try again shortly.",
                headers={"Retry-After": str(BUSY_RETRY_AFTER_S)},
            )

        future: asyncio.Future[RenderedImage] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_retrieve_exception)
        self._inflight[key] = future
        try:
            result = await self._render(params)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            future.set_exception(exc)  # errors are shared with waiters but never cached
            raise
        else:
            self._cache.set(key, result)
            future.set_result(result)
            return result, CacheStatus.MISS
        finally:
            del self._inflight[key]

    async def _render(self, params: ProcessParams) -> RenderedImage:
        video = params.time is not None
        async with self._fetch_slots:
            fetched = await self._fetcher.fetch(params.url, video=video)
        # ffmpeg is as CPU- and memory-hungry as a decode, so it shares the decode slots.
        async with self._decode_slots:
            image_data = fetched.data
            if params.time is not None:
                image_data = await extract_frame(fetched.data, params.time, self._settings)
            out = await run_in_threadpool(process_image, image_data, params, self._settings)
        source = replace(out.source, format="video", bytes=len(fetched.data)) if video else out.source
        etag = '"' + hashlib.sha256(out.data).hexdigest()[:ETAG_HEX_CHARS] + '"'
        return RenderedImage(out.data, out.content_type, out.format, out.width, out.height, etag, source)
