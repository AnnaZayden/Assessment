"""A small typed client for the image service.

from image_client import ImageClient, ImageServiceError

with ImageClient("https://images.example.com") as images:
    thumb_url = images.url("https://example.com/cat.jpg", width=400, format="auto")  # for <img src>
    try:
        result = images.process("https://example.com/cat.jpg", width=400, format="webp")
    except ImageServiceError as err:
        print(err.code, err.param, err.message)  # e.g. "invalid_parameter", "width", "..."
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Final, Literal, Self, TypedDict
from urllib.parse import urlencode

import httpx

__all__ = ["Crop", "Format", "ImageClient", "ImageServiceError", "Limits", "ProcessedImage", "SourceDetails"]

Format = Literal["jpeg", "png", "webp", "gif", "avif", "auto"]
Crop = Literal["fit", "fill", "scale"]

MAX_QUALITY: Final = 100
NETWORK_ERROR: Final = "network_error"
UNEXPECTED_RESPONSE: Final = "unexpected_response"


class Limits(TypedDict):
    limits: dict[str, object]
    parameters: list[str]
    crop_modes: list[str]
    input_formats: list[str]
    output_formats: list[str]


@dataclass(frozen=True)
class SourceDetails:
    format: str
    width: int
    height: int
    bytes: int


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    content_type: str
    width: int
    height: int
    source: SourceDetails
    cache: str  # HIT, MISS or COALESCED
    etag: str
    request_id: str


class ImageServiceError(Exception):
    """Any failure: an API error (``status`` and ``code`` from the response) or a network problem.

    ``code`` is the API's error code (e.g. ``invalid_parameter``), ``network_error`` when the service could
    not be reached, or ``unexpected_response`` for a reply that isn't in the API's error format.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        param: str | None = None,
        upstream_status: int | None = None,
        retry_after_s: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.param = param
        self.upstream_status = upstream_status
        self.retry_after_s = retry_after_s
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        """True when the same request may succeed later (rate limits, busy service, upstream trouble)."""
        if self.status is None:
            return True  # never reached the service
        codes = httpx.codes
        return self.status == codes.TOO_MANY_REQUESTS or self.status >= codes.INTERNAL_SERVER_ERROR


class ImageClient:
    def __init__(self, base_url: str, *, http: httpx.Client | None = None, timeout_s: float = 30.0) -> None:
        """``http`` lets callers share a connection pool or inject a test client; otherwise one is created."""
        self._base_url = base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout_s)

    def url(
        self,
        source: str,
        *,
        width: int | None = None,
        height: int | None = None,
        format: Format | None = None,
        quality: int | None = None,
        crop: Crop | None = None,
    ) -> str:
        """The /process URL for these options, e.g. for an <img src>. Obvious mistakes fail here, early."""
        for name, value, high in (
            ("width", width, None),
            ("height", height, None),
            ("quality", quality, MAX_QUALITY),
        ):
            if value is not None and (value < 1 or (high is not None and value > high)):
                limit = f"between 1 and {high}" if high else "at least 1"
                raise ValueError(f"{name} must be {limit}, got {value}")
        if crop == "fill" and (width is None or height is None):
            raise ValueError("crop='fill' needs both width and height")
        options = {"width": width, "height": height, "format": format, "quality": quality, "crop": crop}
        query = {"url": source, **{k: str(v) for k, v in options.items() if v is not None}}
        return f"{self._base_url}/process?{urlencode(query)}"

    def process(
        self,
        source: str,
        *,
        width: int | None = None,
        height: int | None = None,
        format: Format | None = None,
        quality: int | None = None,
        crop: Crop | None = None,
        accept: str | None = None,
    ) -> ProcessedImage:
        """Fetch the processed image. ``accept`` matters only for format="auto"."""
        url = self.url(source, width=width, height=height, format=format, quality=quality, crop=crop)
        response = self._send(url, headers={"Accept": accept} if accept else {})
        headers = response.headers
        return ProcessedImage(
            data=response.content,
            content_type=headers["content-type"],
            width=int(headers["x-image-width"]),
            height=int(headers["x-image-height"]),
            source=SourceDetails(
                format=headers["x-source-format"],
                width=int(headers["x-source-width"]),
                height=int(headers["x-source-height"]),
                bytes=int(headers["x-source-bytes"]),
            ),
            cache=headers["x-cache"],
            etag=headers["etag"],
            request_id=headers.get("x-request-id", ""),
        )

    def limits(self) -> Limits:
        body: Limits = self._send(f"{self._base_url}/limits").json()
        return body

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def _send(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = self._http.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ImageServiceError(NETWORK_ERROR, f"Could not reach the image service: {exc}") from exc
        if response.is_success:
            return response
        raise _error_from(response)


def _error_from(response: httpx.Response) -> ImageServiceError:
    status = response.status_code
    retry_after = response.headers.get("retry-after", "")
    retry_after_s = int(retry_after) if retry_after.isdigit() else None
    request_id = response.headers.get("x-request-id")
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
    except (ValueError, KeyError, TypeError):
        return ImageServiceError(
            UNEXPECTED_RESPONSE,
            f"HTTP {status} without an API error body.",
            status=status,
            retry_after_s=retry_after_s,
            request_id=request_id,
        )
    param, upstream_status = error.get("param"), error.get("upstream_status")
    return ImageServiceError(
        code,
        message,
        status=status,
        param=param if isinstance(param, str) else None,
        upstream_status=upstream_status if isinstance(upstream_status, int) else None,
        retry_after_s=retry_after_s,
        request_id=request_id,
    )
