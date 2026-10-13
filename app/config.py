"""Service limits and tunables, validated at startup. Every value can be overridden with an env var."""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ENV_PREFIX = "IMG_"
# Hosting platforms set these without our prefix, so they are accepted as aliases (first non-empty wins).
PLATFORM_ALIASES: dict[str, tuple[str, ...]] = {
    "host": ("HOST",),
    "port": ("PORT",),
    "version": ("RENDER_GIT_COMMIT", "GIT_COMMIT"),  # Render sets the first; our Docker build sets the second
}

Positive = Annotated[int, Field(gt=0)]
PositiveSeconds = Annotated[float, Field(gt=0)]


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- Server ------------------------------------------------------------
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8000
    version: str = "dev"  # the deployed commit, reported by /healthz

    # --- Fetching untrusted URLs -------------------------------------------
    max_source_bytes: Positive = 15 * 1024 * 1024
    fetch_timeout_s: PositiveSeconds = 10.0  # wall clock for the whole fetch, incl. redirects
    connect_timeout_s: PositiveSeconds = 3.0
    max_redirects: Annotated[int, Field(ge=0)] = 3
    allowed_ports: frozenset[Annotated[int, Field(ge=1, le=65535)]] = frozenset({80, 443})

    # --- Image processing --------------------------------------------------
    max_input_pixels: Positive = 40_000_000  # ~160 MB decoded as RGBA
    max_output_dimension: Positive = 4000
    default_quality: Annotated[int, Field(ge=1, le=100)] = 80
    max_concurrent_fetches: Positive = 32
    max_concurrent_decodes: Positive = 4
    max_pending_renders: Positive = 128  # beyond this, shed load with 503 instead of queueing

    # --- Caching -----------------------------------------------------------
    cache_max_bytes: Positive = 256 * 1024 * 1024
    cache_ttl_s: Positive = 3600
    http_max_age_s: Annotated[int, Field(ge=0)] = 86400

    # --- Abuse -------------------------------------------------------------
    rate_limit_per_minute: Positive = 60
    rate_limit_burst: Positive = 20
    trust_proxy_headers: bool = False

    @field_validator("allowed_ports", mode="before")
    @classmethod
    def _split_port_list(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def _check_relations(self) -> Self:
        if self.connect_timeout_s > self.fetch_timeout_s:
            raise ValueError("connect_timeout_s cannot be longer than fetch_timeout_s")
        if not self.allowed_ports:
            raise ValueError("allowed_ports cannot be empty")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build settings from IMG_* variables, e.g. IMG_MAX_SOURCE_BYTES=5000000. Bad values fail fast."""
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            keys = (ENV_PREFIX + name.upper(), *PLATFORM_ALIASES.get(name, ()))
            # An empty variable counts as unset, e.g. a build that had no commit to stamp.
            if raw := next((env[key] for key in keys if env.get(key)), None):
                values[name] = raw
        known = {ENV_PREFIX + name.upper() for name in cls.model_fields}
        for unknown in sorted(key for key in env if key.startswith(ENV_PREFIX) and key not in known):
            warnings.warn(f"Ignoring unknown setting {unknown}.", UserWarning, stacklevel=2)
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ValueError(f"Invalid configuration from environment:\n{exc}") from None

    def with_overrides(self, **changes: Any) -> Settings:
        """A validated copy with some values changed (``model_copy`` would skip validation)."""
        return type(self).model_validate({**self.model_dump(), **changes})
