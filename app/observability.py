"""Request IDs, structured (JSON) logs, and Prometheus-format metrics. No external dependencies."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Final

REQUEST_ID_HEADER: Final = "X-Request-ID"
# Only accept request IDs that look safe to echo into headers and log files (no special characters).
_VALID_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
# ContextVar stores the current request ID per-async-task so every log line within a request
# automatically includes it without needing to pass it around explicitly.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Upper bounds (seconds) of the request-latency histogram buckets.
LATENCY_BUCKETS_S: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def accepted_request_id(header: str | None) -> str | None:
    """Reuse a caller's request ID only if it is short and safe to echo into headers and logs."""
    if header and _VALID_REQUEST_ID.fullmatch(header):
        return header
    return None


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Structured fields come from ``extra={"fields": {...}}``."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            entry.update(fields)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


# Libraries that log full request URLs at INFO. Source URLs can carry tokens, so they log warnings only.
URL_LOGGING_LIBRARIES: Final = ("httpx", "httpcore")


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))


class Metrics:
    """In-process counters and a latency histogram, rendered in the Prometheus text format.

    All metrics are plain Python Counters — no external library needed.
    /metrics renders them in the Prometheus text exposition format so any
    Prometheus-compatible scraper (Grafana, Datadog, etc.) can consume them.
    """

    def __init__(self) -> None:
        # Keyed by (route, method, status_code) — e.g. ("/process", "GET", 200)
        self._requests: Counter[tuple[str, str, int]] = Counter()
        # Keyed by error code string — e.g. "rate_limited", "corrupt_image"
        self._errors: Counter[str] = Counter()
        # Keyed by cache status string — "HIT", "MISS", "COALESCED"
        self._cache: Counter[str] = Counter()
        # Histogram: keyed by (route, upper_bound_seconds)
        self._latency_buckets: Counter[tuple[str, float]] = Counter()
        self._latency_sum: defaultdict[str, float] = defaultdict(float)
        self._latency_count: Counter[str] = Counter()

    def observe_request(self, route: str, method: str, status: int, seconds: float) -> None:
        self._requests[route, method, status] += 1
        self._latency_sum[route] += seconds
        self._latency_count[route] += 1
        # A histogram bucket at bound X counts all requests that finished in ≤ X seconds.
        # Prometheus expects cumulative counts (each bucket includes all faster ones).
        for bound in LATENCY_BUCKETS_S:
            if seconds <= bound:
                self._latency_buckets[route, bound] += 1

    def observe_error(self, code: str) -> None:
        self._errors[code] += 1

    def observe_cache(self, status: str) -> None:
        self._cache[status] += 1

    def render(self, gauges: Mapping[str, float]) -> str:
        lines = [
            "# TYPE http_requests_total counter",
            *(
                f'http_requests_total{{route="{route}",method="{method}",status="{status}"}} {n}'
                for (route, method, status), n in sorted(self._requests.items())
            ),
            "# TYPE http_request_duration_seconds histogram",
        ]
        for route in sorted(self._latency_count):
            lines += [
                f'http_request_duration_seconds_bucket{{route="{route}",le="{bound}"}} '
                f"{self._latency_buckets[route, bound]}"
                for bound in LATENCY_BUCKETS_S
            ]
            count = self._latency_count[route]
            lines += [
                f'http_request_duration_seconds_bucket{{route="{route}",le="+Inf"}} {count}',
                f'http_request_duration_seconds_sum{{route="{route}"}} {self._latency_sum[route]:.6f}',
                f'http_request_duration_seconds_count{{route="{route}"}} {count}',
            ]
        lines += ["# TYPE image_errors_total counter"]
        lines += [f'image_errors_total{{code="{code}"}} {n}' for code, n in sorted(self._errors.items())]
        lines += ["# TYPE image_cache_total counter"]
        lines += [f'image_cache_total{{status="{status}"}} {n}' for status, n in sorted(self._cache.items())]
        for name, value in sorted(gauges.items()):
            lines += [f"# TYPE {name} gauge", f"{name} {value:g}"]
        return "\n".join(lines) + "\n"
