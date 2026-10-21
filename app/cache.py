"""In-process LRU cache for processed images, bounded by total bytes and entry age."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol


class Sized(Protocol):
    """Anything the cache can hold must expose its byte size."""
    @property
    def size(self) -> int: ...


class LRUCache[V: Sized]:
    """Least-Recently-Used cache bounded by total bytes (not entry count) and a TTL per entry.

    Uses an OrderedDict to keep entries in access order cheaply.
    The clock is injectable so tests can simulate time without sleeping.
    """

    def __init__(self, max_bytes: int, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._max_bytes = max_bytes
        self._ttl_s = ttl_s
        self._clock = clock
        # Each entry stores (stored_at_timestamp, value). OrderedDict maintains insertion/access order.
        self._entries: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._bytes = 0  # running total of bytes stored, so we don't sum sizes on every set()

    def get(self, key: str) -> V | None:
        item = self._entries.get(key)
        if item is None:
            return None
        stored_at, value = item
        # Lazy expiry: check the TTL on read rather than running a background cleanup task.
        if self._clock() - stored_at > self._ttl_s:
            self._remove(key)
            return None
        # Move to end = mark as most recently used, so it's the last to be evicted.
        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: V) -> None:
        # Don't cache a single item that's larger than the whole cache — it would immediately evict everything.
        if value.size > self._max_bytes:
            return
        if key in self._entries:
            self._remove(key)  # update: remove old size before adding new
        self._entries[key] = (self._clock(), value)
        self._bytes += value.size
        # Evict the least-recently-used entries until we're back under the byte limit.
        while self._bytes > self._max_bytes:
            self._remove(next(iter(self._entries)))

    def _remove(self, key: str) -> None:
        _, value = self._entries.pop(key)
        self._bytes -= value.size

    def __len__(self) -> int:
        return len(self._entries)
