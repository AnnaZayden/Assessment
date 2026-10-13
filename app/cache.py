"""In-process LRU cache for processed images, bounded by total bytes and entry age."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol


class Sized(Protocol):
    @property
    def size(self) -> int: ...


class LRUCache[V: Sized]:
    def __init__(self, max_bytes: int, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._max_bytes = max_bytes
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._bytes = 0

    def get(self, key: str) -> V | None:
        item = self._entries.get(key)
        if item is None:
            return None
        stored_at, value = item
        if self._clock() - stored_at > self._ttl_s:
            self._remove(key)
            return None
        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: V) -> None:
        if value.size > self._max_bytes:
            return
        if key in self._entries:
            self._remove(key)
        self._entries[key] = (self._clock(), value)
        self._bytes += value.size
        while self._bytes > self._max_bytes:
            self._remove(next(iter(self._entries)))

    def _remove(self, key: str) -> None:
        _, value = self._entries.pop(key)
        self._bytes -= value.size

    def __len__(self) -> int:
        return len(self._entries)
