"""Every failure the API can report, and the single JSON shape they all render as:

{"error": {"code": "invalid_parameter", "message": "...", "param": "width"}}
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import NotRequired, TypedDict


class ErrorCode(Enum):
    """Public error codes, each tied to the one HTTP status it is sent with."""

    # 4xx: the caller can fix the request.
    MISSING_PARAMETER = ("missing_parameter", 400)
    INVALID_PARAMETER = ("invalid_parameter", 400)
    UNKNOWN_PARAMETER = ("unknown_parameter", 400)
    DUPLICATE_PARAMETER = ("duplicate_parameter", 400)
    INVALID_URL = ("invalid_url", 400)
    URL_NOT_ALLOWED = ("url_not_allowed", 403)
    NOT_FOUND = ("not_found", 404)
    METHOD_NOT_ALLOWED = ("method_not_allowed", 405)
    SOURCE_TOO_LARGE = ("source_too_large", 413)
    UNSUPPORTED_MEDIA_TYPE = ("unsupported_media_type", 415)
    SOURCE_TOO_MANY_PIXELS = ("source_too_many_pixels", 422)
    OUTPUT_TOO_LARGE = ("output_too_large", 422)
    CORRUPT_IMAGE = ("corrupt_image", 422)
    RATE_LIMITED = ("rate_limited", 429)
    # 5xx: our side, or the source's side; retrying later may work.
    INTERNAL_ERROR = ("internal_error", 500)
    NOT_IMPLEMENTED = ("not_implemented", 501)
    UPSTREAM_ERROR = ("upstream_error", 502)
    UPSTREAM_UNREACHABLE = ("upstream_unreachable", 502)
    DNS_ERROR = ("dns_error", 502)
    TOO_MANY_REDIRECTS = ("too_many_redirects", 502)
    SERVICE_BUSY = ("service_busy", 503)
    UPSTREAM_TIMEOUT = ("upstream_timeout", 504)

    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status


class ErrorDetail(TypedDict):
    code: str
    message: str
    param: NotRequired[str]
    upstream_status: NotRequired[int]


class ErrorBody(TypedDict):
    error: ErrorDetail


class ApiError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        param: str | None = None,
        upstream_status: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.param = param
        self.upstream_status = upstream_status
        self.headers: Mapping[str, str] = headers or {}

    @property
    def status(self) -> int:
        return self.code.status

    def to_body(self) -> ErrorBody:
        error: ErrorDetail = {"code": self.code.code, "message": self.message}
        if self.param is not None:
            error["param"] = self.param
        if self.upstream_status is not None:
            error["upstream_status"] = self.upstream_status
        return {"error": error}
