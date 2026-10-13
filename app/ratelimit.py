"""Per-client token bucket. In-memory, so per-process; see README for the multi-instance story."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

SECONDS_PER_MINUTE: Final = 60.0
SWEEP_EVERY_CHECKS: Final = 1000  # how often idle buckets are dropped; cheap, and bounds memory


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: int
    retry_after_s: int


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        sweep_every: int = SWEEP_EVERY_CHECKS,
    ) -> None:
        if per_minute <= 0 or burst <= 0:
            raise ValueError("per_minute and burst must be positive")
        self._rate = per_minute / SECONDS_PER_MINUTE
        self._burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._sweep_every = sweep_every
        self._calls = 0

    @property
    def tracked_clients(self) -> int:
        """Clients with a bucket in memory (idle ones are swept periodically)."""
        return len(self._buckets)

    def check(self, key: str) -> RateDecision:
        now = self._clock()
        self._maybe_sweep(now)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(self._burst, now)
        else:
            bucket.tokens = min(self._burst, bucket.tokens + (now - bucket.updated) * self._rate)
            bucket.updated = now

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return RateDecision(True, int(bucket.tokens), 0)
        retry_after = math.ceil((1 - bucket.tokens) / self._rate)
        return RateDecision(False, 0, retry_after)

    def _maybe_sweep(self, now: float) -> None:
        """Drop buckets that have refilled completely, so memory stays bounded by active clients."""
        self._calls += 1
        if self._calls % self._sweep_every:
            return
        full_after = self._burst / self._rate
        stale = [k for k, b in self._buckets.items() if now - b.updated >= full_after]
        for key in stale:
            del self._buckets[key]
