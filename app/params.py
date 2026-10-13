"""The /process query string: one schema behind validation, the OpenAPI docs, and /limits."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from .config import Settings
from .errors import ApiError, ErrorCode
from .formats import FORMAT_ALIASES, MIME_TYPES, NEGOTIABLE_FORMATS, OUTPUT_FORMATS, OutputFormat
from .urls import url_problem

CropMode = Literal["fit", "fill", "scale"]
CROP_MODES: Final[tuple[CropMode, ...]] = get_args(CropMode)
AUTO_FORMAT: Final = "auto"
MAX_QUALITY: Final = 100
# Every numeric limit fits in 6 digits; refusing longer strings keeps int() cheap on hostile input.
MAX_INT_DIGITS: Final = 6
URL_REQUIRED: Final = (
    "The 'url' query parameter is required, e.g. /process?url=https://example.com/cat.jpg&width=400."
)


@dataclass(frozen=True)
class ProcessParams:
    """A validated request, with defaults applied and format=auto already resolved."""

    url: str
    quality: int
    width: int | None = None
    height: int | None = None
    format: OutputFormat | None = None  # None = keep the source format when we can encode it
    crop: CropMode = "fit"
    negotiated: bool = False  # the format came from the Accept header, so the response varies by it

    def cache_key(self) -> str:
        return "\x1f".join(
            str(v) for v in (self.url, self.width, self.height, self.format, self.quality, self.crop)
        )


def _settings(info: ValidationInfo) -> Settings:
    settings = info.context.get("settings") if isinstance(info.context, dict) else None
    if not isinstance(settings, Settings):
        raise TypeError("ProcessQuery must be validated with context={'settings': Settings(...)}")
    return settings


def _error(code: ErrorCode, message: str) -> PydanticCustomError:
    # The message goes in through the context so braces in user input are never treated as a template.
    return PydanticCustomError(code.code, "{message}", {"message": message})


class ProcessQuery(BaseModel):
    """The raw query parameters. Values arrive as strings and are checked against the active Settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(description="http(s) URL of the source image.")
    width: int | None = Field(None, description="Output width in pixels, 1 to the configured maximum.")
    height: int | None = Field(None, description="Output height in pixels, 1 to the configured maximum.")
    format: OutputFormat | Literal["auto"] | None = Field(
        None,
        description="Output format (`jpg` is accepted for jpeg). `auto` picks AVIF or WebP from the Accept "
        "header. Default: the source format.",
    )
    quality: int | None = Field(
        None,
        description="1-100, for jpeg, webp and avif. Default: the server's default_quality (see /limits).",
    )
    crop: CropMode | None = Field(
        None, description="fit (default): inside the box. fill: cover and centre-crop. scale: stretch."
    )

    @field_validator("url", mode="before")
    @classmethod
    def _check_url(cls, value: str, info: ValidationInfo) -> str:
        if not value:
            raise _error(ErrorCode.MISSING_PARAMETER, URL_REQUIRED)
        if problem := url_problem(value, _settings(info)):
            raise _error(ErrorCode.INVALID_URL, problem)
        return value

    @field_validator("width", "height", "quality", mode="before")
    @classmethod
    def _check_int(cls, value: str, info: ValidationInfo) -> int:
        name = str(info.field_name)
        high = MAX_QUALITY if name == "quality" else _settings(info).max_output_dimension
        if not _is_plain_int(value) or not 1 <= int(value) <= high:
            raise _error(
                ErrorCode.INVALID_PARAMETER,
                f"'{name}' must be an integer between 1 and {high}, got '{value}'.",
            )
        return int(value)

    @field_validator("format", mode="before")
    @classmethod
    def _check_format(cls, value: str) -> str:
        name = FORMAT_ALIASES.get(value.lower(), value.lower())
        if name != AUTO_FORMAT and name not in OUTPUT_FORMATS:
            supported = ", ".join([*OUTPUT_FORMATS, AUTO_FORMAT])
            raise _error(
                ErrorCode.INVALID_PARAMETER, f"Unsupported format '{value}'. Supported: {supported}."
            )
        return name

    @field_validator("crop", mode="before")
    @classmethod
    def _check_crop(cls, value: str, info: ValidationInfo) -> str:
        mode = value.lower()
        if mode not in CROP_MODES:
            raise _error(
                ErrorCode.INVALID_PARAMETER, f"'crop' must be one of {', '.join(CROP_MODES)}, got '{value}'."
            )
        if mode == "fill" and (info.data.get("width") is None or info.data.get("height") is None):
            raise _error(ErrorCode.INVALID_PARAMETER, "crop=fill needs both 'width' and 'height'.")
        return mode


PARAMETER_NAMES: Final[tuple[str, ...]] = tuple(ProcessQuery.model_fields)
_CUSTOM_CODES: Final = {
    c.code: c for c in (ErrorCode.MISSING_PARAMETER, ErrorCode.INVALID_PARAMETER, ErrorCode.INVALID_URL)
}


def _is_plain_int(raw: str) -> bool:
    """ASCII digits only: no sign, decimal point, whitespace, or non-Latin digits that int() would accept."""
    return raw.isascii() and raw.isdigit() and len(raw) <= MAX_INT_DIGITS


def parse_process_params(
    items: Iterable[tuple[str, str]], settings: Settings, accept: str | None = None
) -> ProcessParams:
    values: dict[str, str] = {}
    for key, value in items:
        if key in values:
            raise ApiError(
                ErrorCode.DUPLICATE_PARAMETER, f"Query parameter '{key}' was given more than once.", param=key
            )
        values[key] = value.strip()
    try:
        query = ProcessQuery.model_validate(values, context={"settings": settings})
    except ValidationError as exc:
        raise _to_api_error(exc) from None

    fmt, negotiated = _resolve_format(query.format, accept)
    return ProcessParams(
        url=query.url,
        quality=query.quality or settings.default_quality,
        width=query.width,
        height=query.height,
        format=fmt,
        crop=query.crop or "fit",
        negotiated=negotiated,
    )


def _to_api_error(exc: ValidationError) -> ApiError:
    # Report one problem at a time, unknown parameters first: they usually explain the others (typos).
    first = min(exc.errors(), key=lambda e: e["type"] != "extra_forbidden")
    param = str(first["loc"][0])
    if first["type"] == "extra_forbidden":
        allowed = ", ".join(PARAMETER_NAMES)
        return ApiError(
            ErrorCode.UNKNOWN_PARAMETER,
            f"Unknown query parameter '{param}'. Allowed parameters: {allowed}.",
            param=param,
        )
    if first["type"] == "missing":
        return ApiError(ErrorCode.MISSING_PARAMETER, URL_REQUIRED, param=param)
    return ApiError(_CUSTOM_CODES.get(first["type"], ErrorCode.INVALID_PARAMETER), first["msg"], param=param)


def _resolve_format(
    requested: OutputFormat | Literal["auto"] | None, accept: str | None
) -> tuple[OutputFormat | None, bool]:
    if requested != AUTO_FORMAT:
        return requested, False
    accepted = accepted_media_types(accept)
    # No negotiable format accepted: keep the source format, which every client can already display.
    return next((f for f in NEGOTIABLE_FORMATS if MIME_TYPES[f] in accepted), None), True


def accepted_media_types(header: str | None) -> set[str]:
    """Media types in an Accept header that the client accepts (q > 0). Wildcards are kept as written."""
    accepted = set()
    for item in (header or "").split(","):
        media_type, *parameters = (part.strip() for part in item.split(";"))
        quality = 1.0
        for parameter in parameters:
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "q":
                quality = _parse_q(value)
        if media_type and quality > 0:
            accepted.add(media_type.lower())
    return accepted


def _parse_q(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0


def openapi_parameters() -> list[dict[str, Any]]:
    """OpenAPI query parameters generated from ProcessQuery, so the docs cannot drift from validation."""
    schema = ProcessQuery.model_json_schema()
    return [
        {
            "name": name,
            "in": "query",
            "required": name in schema.get("required", []),
            "description": field.description,
            "schema": {k: v for k, v in schema["properties"][name].items() if k != "description"},
        }
        for name, field in ProcessQuery.model_fields.items()
    ]
