"""Routes. Each one reads the shared services through the ``Ctx`` dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Final, TypedDict
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

from .config import Settings
from .errors import ApiError, ErrorCode
from .formats import INPUT_FORMATS, OUTPUT_FORMATS
from .observability import Metrics
from .params import CROP_MODES, PARAMETER_NAMES, ProcessParams, openapi_parameters, parse_process_params
from .ratelimit import RateLimiter
from .service import CacheStatus, ImageService, RenderedImage

STATIC_DIR: Final = Path(__file__).resolve().parent.parent / "static"


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    service: ImageService
    limiter: RateLimiter
    metrics: Metrics


def get_context(request: Request) -> AppContext:
    context: AppContext = request.app.state.context
    return context


Ctx = Annotated[AppContext, Depends(get_context)]


class LimitsBody(TypedDict):
    limits: dict[str, object]
    parameters: list[str]
    crop_modes: list[str]
    input_formats: list[str]
    output_formats: list[str]


@dataclass
class RequestInfo:
    """What one request learns along the way, for its response headers and its access-log line."""

    rate_headers: dict[str, str] = field(default_factory=dict)  # sent on successes and errors alike
    log_fields: dict[str, object] = field(default_factory=dict)


def request_info(request: Request) -> RequestInfo:
    info = getattr(request.state, "info", None)
    if not isinstance(info, RequestInfo):
        info = request.state.info = RequestInfo()
    return info


def enforce_rate_limit(request: Request, ctx: Ctx) -> None:
    decision = ctx.limiter.check(client_ip(request, ctx.settings))
    request_info(request).rate_headers = {
        "X-RateLimit-Limit": str(ctx.settings.rate_limit_per_minute),
        "X-RateLimit-Remaining": str(decision.remaining),
    }
    if not decision.allowed:
        raise ApiError(
            ErrorCode.RATE_LIMITED,
            f"Too many requests. Try again in {decision.retry_after_s} seconds.",
            headers={"Retry-After": str(decision.retry_after_s)},
        )


router = APIRouter()


@router.get(
    "/process",
    summary="Fetch, resize, and/or convert an image",
    dependencies=[Depends(enforce_rate_limit)],
    responses={200: {"content": {"image/*": {}}, "description": "The processed image bytes."}},
    openapi_extra={"parameters": openapi_parameters()},
)
async def process(request: Request, ctx: Ctx) -> Response:
    params = parse_process_params(
        request.query_params.multi_items(), ctx.settings, request.headers.get("accept")
    )
    log_fields = request_info(request).log_fields
    log_fields["source_host"] = source_host(params.url)
    image, cache_status = await ctx.service.get(params)
    ctx.metrics.observe_cache(cache_status)
    log_fields["cache"] = cache_status
    return image_response(request, image, cache_status, params, ctx.settings)


@router.get("/video/thumbnail", summary="Not implemented (see README)")
async def video_thumbnail() -> Response:
    raise ApiError(
        ErrorCode.NOT_IMPLEMENTED, "Video thumbnails are not implemented in this version; see the README."
    )


@router.get("/limits", summary="The limits this instance enforces")
async def limits(ctx: Ctx) -> LimitsBody:
    data = ctx.settings.model_dump(mode="json", exclude={"host", "port", "version", "trust_proxy_headers"})
    data["allowed_ports"] = sorted(ctx.settings.allowed_ports)
    return {
        "limits": data,
        "parameters": list(PARAMETER_NAMES),
        "crop_modes": list(CROP_MODES),
        "input_formats": [f.lower() for f in INPUT_FORMATS],
        "output_formats": list(OUTPUT_FORMATS),
    }


@router.get("/healthz", include_in_schema=False)
async def healthz(ctx: Ctx) -> dict[str, str]:
    """Liveness, plus the running commit so a deploy can confirm the new version is live."""
    return {"status": "ok", "version": ctx.settings.version}


@router.get("/metrics", include_in_schema=False)
async def metrics(ctx: Ctx) -> PlainTextResponse:
    gauges = {
        "image_renders_in_flight": float(ctx.service.in_flight),
        "rate_limit_tracked_clients": float(ctx.limiter.tracked_clients),
    }
    return PlainTextResponse(ctx.metrics.render(gauges), media_type="text/plain; version=0.0.4")


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def image_response(
    request: Request,
    image: RenderedImage,
    cache_status: CacheStatus,
    params: ProcessParams,
    settings: Settings,
) -> Response:
    headers = {
        **request_info(request).rate_headers,
        "Cache-Control": f"public, max-age={settings.http_max_age_s}",
        "ETag": image.etag,
        "X-Cache": cache_status.value,
        "X-Image-Width": str(image.width),
        "X-Image-Height": str(image.height),
        "X-Source-Format": image.source.format,
        "X-Source-Width": str(image.source.width),
        "X-Source-Height": str(image.source.height),
        "X-Source-Bytes": str(image.source.bytes),
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="image.{image.format}"',
    }
    if params.negotiated:
        headers["Vary"] = "Accept"  # the same URL gives different bytes to different browsers
    if etag_matches(request.headers.get("if-none-match"), image.etag):
        return Response(status_code=304, headers=headers)
    return Response(image.data, media_type=image.content_type, headers=headers)


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers and (forwarded := request.headers.get("x-forwarded-for")):
        # The right-most entry is the one our own proxy appended; the rest are client-controlled.
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = {tag.strip().removeprefix("W/") for tag in header.split(",")}
    return "*" in candidates or etag in candidates


def source_host(url: str) -> str:
    """Log only the host. Full URLs can carry tokens or personal data in paths and query strings."""
    return urlsplit(url).hostname or "?"
