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

# 32 hex chars = 128 bits of SHA-256. Enough to be collision-safe while keeping response headers short.
ETAG_HEX_CHARS: Final = 32
BUSY_RETRY_AFTER_S: Final = 1


class CacheStatus(StrEnum):
    HIT = "HIT"        # result was already in the LRU cache
    MISS = "MISS"      # we fetched and processed it fresh
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
        # _inflight maps a cache key → future for requests currently being processed.
        # Used for coalescing: if a second request arrives for the same image, it awaits this future
        # instead of starting a duplicate fetch+decode.
        self._inflight: dict[str, asyncio.Future[RenderedImage]] = {}
        # Semaphores cap concurrent resource usage so one burst can't starve everything else.
        self._fetch_slots = asyncio.Semaphore(settings.max_concurrent_fetches)   # limits outbound HTTP
        self._decode_slots = asyncio.Semaphore(settings.max_concurrent_decodes)  # limits CPU/memory

    @property
    def in_flight(self) -> int:
        """Distinct renders currently running or queued for a slot."""
        return len(self._inflight)

    async def get(self, params: ProcessParams) -> tuple[RenderedImage, CacheStatus]:
        key = params.cache_key()

        # Fast path: already processed and still in the LRU cache.
        if (cached := self._cache.get(key)) is not None:
            return cached, CacheStatus.HIT

        # Coalescing: the exact same image+params is already being fetched by another request.
        # asyncio.shield keeps the future alive even if this particular caller gets cancelled.
        if (pending := self._inflight.get(key)) is not None:
            return await asyncio.shield(pending), CacheStatus.COALESCED

        # Load shedding: too many renders queued already → return 503 instead of piling on.
        if len(self._inflight) >= self._settings.max_pending_renders:
            raise ApiError(
                ErrorCode.SERVICE_BUSY,
                "The service is at capacity. Try again shortly.",
                headers={"Retry-After": str(BUSY_RETRY_AFTER_S)},
            )

        # Register the future *before* awaiting so any concurrent identical request can coalesce onto it.
        future: asyncio.Future[RenderedImage] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_retrieve_exception)
        self._inflight[key] = future
        try:
            result = await self._render(params)
        except asyncio.CancelledError:
            # Client disconnected: cancel the shared future so coalesced waiters also unblock.
            future.cancel()
            raise
        except Exception as exc:
            # Propagate the error to any coalesced waiters, but do NOT cache it.
            future.set_exception(exc)
            raise
        else:
            self._cache.set(key, result)
            future.set_result(result)  # unblocks any coalesced waiters
            return result, CacheStatus.MISS
        finally:
            # Always clean up the in-flight entry so future requests start fresh.
            del self._inflight[key]

    async def _render(self, params: ProcessParams) -> RenderedImage:
        video = params.time is not None
        # Fetch the raw bytes from the URL (bounded by max_concurrent_fetches).
        async with self._fetch_slots:
            fetched = await self._fetcher.fetch(params.url, video=video)
        # ffmpeg is as CPU- and memory-hungry as a decode, so it shares the decode slots.
        async with self._decode_slots:
            image_data = fetched.data
            if params.time is not None:
                # Extract one frame from the video as PNG bytes, then process it like any image.
                image_data = await extract_frame(fetched.data, params.time, self._settings)
            # process_image is CPU-bound (Pillow); run_in_threadpool moves it off the async event loop
            # so it doesn't block other concurrent requests while it runs.
            out = await run_in_threadpool(process_image, image_data, params, self._settings)
        # For video requests, report the original video size (not the extracted frame size) in source info.
        source = replace(out.source, format="video", bytes=len(fetched.data)) if video else out.source
        # ETag is a hash of the output bytes, used for HTTP 304 Not Modified responses.
        etag = '"' + hashlib.sha256(out.data).hexdigest()[:ETAG_HEX_CHARS] + '"'
        return RenderedImage(out.data, out.content_type, out.format, out.width, out.height, etag, source)
