"""Application factory: wires settings, services, routes, error rendering, and request observation."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import AppContext, get_context, request_info, router
from .config import Settings
from .errors import ApiError, ErrorCode
from .fetcher import Fetcher
from .observability import REQUEST_ID_HEADER, Metrics, accepted_request_id, request_id_var
from .ratelimit import RateLimiter
from .service import ImageService

log = logging.getLogger("image_service")

MS_PER_SECOND: Final = 1000
# Maps FastAPI/Starlette's numeric HTTP errors to our typed error codes.
FRAMEWORK_ERRORS: Final = {404: ErrorCode.NOT_FOUND, 405: ErrorCode.METHOD_NOT_ALLOWED}


def create_app(settings: Settings | None = None, fetcher: Fetcher | None = None) -> FastAPI:
    """Build and return the FastAPI application.

    ``settings`` and ``fetcher`` can be injected in tests to swap out real
    configuration and real HTTP calls for controlled fakes.
    """
    # Read all configuration from environment variables (IMG_* prefix).
    settings = settings or Settings.from_env()
    # Fetcher owns the HTTP client that downloads source images/videos.
    fetcher = fetcher or Fetcher(settings)
    # AppContext is a single object that holds every shared component.
    # It is stored on app.state so any route handler can reach it via the Ctx dependency.
    context = AppContext(
        settings=settings,
        service=ImageService(settings, fetcher),  # orchestrates fetch → process → cache
        limiter=RateLimiter(settings.rate_limit_per_minute, settings.rate_limit_burst),
        metrics=Metrics(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # yield = server is running; after yield = server is shutting down.
        yield
        # Close the underlying httpx client so in-flight connections are cleaned up gracefully.
        await fetcher.aclose()

    app = FastAPI(title="Image Processing Service", version="1.0.0", lifespan=lifespan)
    app.state.context = context
    app.include_router(router)
    _install_error_handlers(app)
    # observe_request wraps every request: request ID, timing, structured log line, metrics.
    app.middleware("http")(observe_request)
    return app


def error_response(request: Request, exc: ApiError) -> JSONResponse:
    """The one place errors become responses: same JSON shape, never cached, always counted."""
    # Record which error code fired so it shows up in /metrics under image_errors_total.
    get_context(request).metrics.observe_error(exc.code.code)
    info = request_info(request)
    info.log_fields["code"] = exc.code.code
    # Rate limit headers are included on errors too so clients know their remaining quota.
    headers = {**info.rate_headers, **exc.headers, "Cache-Control": "no-store"}
    return JSONResponse(exc.to_body(), status_code=exc.status, headers=headers)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        # All intentional errors (bad params, SSRF rejection, etc.) go through here.
        return error_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def handle_framework_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Starlette's router only raises 404 and 405; anything else would be a bug on our side.
        code = FRAMEWORK_ERRORS.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return error_response(request, ApiError(code, str(exc.detail)))


async def observe_request(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Outermost middleware: attaches a request ID, catches unhandled exceptions, and emits one log line."""
    # Reuse the caller's request ID if it looks safe, otherwise generate a fresh UUID.
    # This lets callers correlate their own traces with our logs.
    request_id = accepted_request_id(request.headers.get(REQUEST_ID_HEADER)) or uuid.uuid4().hex
    # Store the request ID in a ContextVar so every log line within this request automatically includes it.
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception:
            # Last-resort handler: any unhandled exception becomes a 500 instead of crashing the server.
            log.exception("unhandled error")
            response = error_response(
                request, ApiError(ErrorCode.INTERNAL_ERROR, "Something went wrong on our side.")
            )
        elapsed = time.perf_counter() - started
        # Use the route template (e.g. "/process") not the raw path, so metrics don't explode with unique URLs.
        route = getattr(request.scope.get("route"), "path", "unmatched")
        get_context(request).metrics.observe_request(route, request.method, response.status_code, elapsed)
        fields = {
            "method": request.method,
            "route": route,
            "status": response.status_code,
            "ms": round(elapsed * MS_PER_SECOND),
        }
        # One structured JSON log line per request, merged with any fields the route handler added.
        log.info("request", extra={"fields": {**fields, **request_info(request).log_fields}})
        # Echo the request ID back in the response so clients can link their request to our logs.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        # Always restore the ContextVar, even if something above raised, to avoid leaking the ID.
        request_id_var.reset(token)
