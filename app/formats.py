"""Which image formats we accept and emit."""

from __future__ import annotations

from typing import Final, Literal

from PIL import features

OutputFormat = Literal["jpeg", "png", "webp", "gif", "avif"]

# Pillow builds without AVIF support return False here (with a warning if they don't know the name).
_AVIF: Final = bool(features.check("avif"))

# Pillow format names we are willing to *decode*. Restricting this list keeps
# Pillow from trying its dozens of other (rarely audited) parsers on hostile
# input. TIFF and SVG are deliberately excluded; see README.
INPUT_FORMATS: Final[tuple[str, ...]] = ("JPEG", "PNG", "WEBP", "GIF", "BMP", *(("AVIF",) if _AVIF else ()))

# Public format name -> Pillow format name, for *encoding*.
OUTPUT_FORMATS: Final[dict[OutputFormat, str]] = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF"}
if _AVIF:
    OUTPUT_FORMATS["avif"] = "AVIF"

FORMAT_ALIASES: Final[dict[str, OutputFormat]] = {"jpg": "jpeg"}

MIME_TYPES: Final[dict[OutputFormat, str]] = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "avif": "image/avif",
}

LOSSY_FORMATS: Final[frozenset[OutputFormat]] = frozenset({"jpeg", "webp", "avif"})

# Encoders that have no greyscale modes, so grey input must become RGB before encoding.
RGB_ONLY_FORMATS: Final[frozenset[OutputFormat]] = frozenset({"webp", "avif"})

# format=auto: the best format the client says it accepts, in order of preference.
NEGOTIABLE_FORMATS: Final[tuple[OutputFormat, ...]] = tuple(
    f for f in ("avif", "webp") if f in OUTPUT_FORMATS
)
