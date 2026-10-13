"""Settings: environment parsing, validation at startup, and the entry point."""

from __future__ import annotations

import runpy

import pytest
import uvicorn
from pydantic import ValidationError

from app.config import Settings
from app.ratelimit import RateLimiter

# --- Settings -------------------------------------------------------------------------


def test_settings_from_env():
    s = Settings.from_env(
        {
            "IMG_ALLOWED_PORTS": "80, 8443,",
            "IMG_TRUST_PROXY_HEADERS": "true",
            "IMG_MAX_SOURCE_BYTES": "1000",
            "IMG_FETCH_TIMEOUT_S": "2.5",
            "IMG_CONNECT_TIMEOUT_S": "1",
            "PORT": "9000",  # platform alias
        }
    )
    assert s.allowed_ports == frozenset({80, 8443})
    assert s.trust_proxy_headers is True
    assert (s.max_source_bytes, s.fetch_timeout_s, s.connect_timeout_s, s.port) == (1000, 2.5, 1.0, 9000)
    assert s.max_redirects == Settings().max_redirects  # untouched fields keep defaults


def test_prefixed_setting_wins_over_platform_alias():
    assert Settings.from_env({"IMG_PORT": "9001", "PORT": "9000"}).port == 9001


@pytest.mark.parametrize("raw", ["false", "0", "no", "False", "off"])
def test_settings_bool_false_spellings(raw):
    assert Settings.from_env({"IMG_TRUST_PROXY_HEADERS": raw}).trust_proxy_headers is False


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({"IMG_MAX_SOURCE_BYTES": "lots"}, "max_source_bytes"),
        ({"IMG_RATE_LIMIT_PER_MINUTE": "0"}, "rate_limit_per_minute"),
        ({"IMG_MAX_REDIRECTS": "-1"}, "max_redirects"),
        ({"IMG_DEFAULT_QUALITY": "101"}, "default_quality"),
        ({"IMG_ALLOWED_PORTS": "80,70000"}, "allowed_ports"),
        ({"IMG_ALLOWED_PORTS": ","}, "allowed_ports cannot be empty"),
        ({"IMG_TRUST_PROXY_HEADERS": "maybe"}, "trust_proxy_headers"),
        ({"IMG_CONNECT_TIMEOUT_S": "20"}, "connect_timeout_s cannot be longer"),
    ],
)
def test_invalid_settings_fail_fast(env, fragment):
    with pytest.raises(ValueError, match="Invalid configuration") as exc:
        Settings.from_env(env)
    assert fragment in str(exc.value)


def test_unknown_setting_warns():
    with pytest.warns(UserWarning, match="IMG_MAX_SORCE_BYTES"):
        Settings.from_env({"IMG_MAX_SORCE_BYTES": "5"})


def test_with_overrides_validates():
    assert Settings().with_overrides(max_redirects=5).max_redirects == 5
    with pytest.raises(ValidationError):
        Settings().with_overrides(max_redirects=-1)


def test_rate_limiter_rejects_non_positive_rates():
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(per_minute=0, burst=1)


# --- Entrypoint ---------------------------------------------------------------------------


def test_main_module_starts_uvicorn_without_access_log(monkeypatch):
    calls = []
    configured = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setattr("app.observability.configure_logging", lambda: configured.append(True))
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9123")
    runpy.run_module("app", run_name="__main__")
    ((app, kwargs),) = calls
    assert kwargs == {"host": "0.0.0.0", "port": 9123, "access_log": False}
    assert configured == [True]
    assert "/process" in app.openapi()["paths"]
