"""Helpers shared by the test modules."""

from __future__ import annotations

import io
from typing import Any

import httpx
from PIL import Image


def make_image(
    fmt: str = "JPEG", size: tuple[int, int] = (800, 600), mode: str = "RGB", **save: object
) -> bytes:
    color = (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)
    return encode(Image.new(mode, size, color), fmt, **save)


def encode(img: Image.Image, fmt: str, **save: object) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **save)
    return buf.getvalue()


def open_image(content: bytes) -> Image.Image:
    return Image.open(io.BytesIO(content))


def error_of(response: httpx.Response) -> dict[str, Any]:
    """The error object of a JSON error response, after checking the shape every error must have."""
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    error: dict[str, Any] = response.json()["error"]
    return error
