"""Decode, transform, and re-encode an image. Pure and synchronous (runs in a worker thread)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Final, Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings
from .errors import ApiError, ErrorCode
from .formats import INPUT_FORMATS, LOSSY_FORMATS, MIME_TYPES, OUTPUT_FORMATS, RGB_ONLY_FORMATS, OutputFormat
from .params import ProcessParams

# LANCZOS gives the best quality when downscaling (sharp edges, no aliasing).
RESAMPLE: Final = Image.Resampling.LANCZOS
# reducing_gap=3 pre-shrinks the image in steps before full LANCZOS, making large downscales
# much faster while being visually indistinguishable from a full-quality resize.
REDUCING_GAP: Final = 3.0
WEBP_METHOD: Final = 4  # 0 = fastest .. 6 = smallest; 4 is Pillow's default trade-off
PNG_COMPRESS_LEVEL: Final = 6  # zlib's default trade-off (speed vs. file size)
WHITE: Final = (255, 255, 255)
SIXTEEN_TO_EIGHT_BITS: Final = 256  # divisor to scale 16-bit grey samples down to 8 bits
# All the errors Pillow can raise for truncated or malformed image data, at open() or load() time.
DECODE_ERRORS: Final = (OSError, ValueError, SyntaxError, EOFError)

ColorFamily = Literal["gray", "rgb", "other"]


@dataclass(frozen=True)
class SourceInfo:
    """What the caller sent us, so clients can compare it with the output."""

    format: str
    width: int  # after EXIF orientation, i.e. as displayed
    height: int
    bytes: int


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    format: OutputFormat
    content_type: str
    width: int
    height: int
    source: SourceInfo


def process_image(data: bytes, params: ProcessParams, settings: Settings) -> ProcessedImage:
    """Main entry point: decode → normalise → resize → strip metadata → encode."""
    img, source = _decode(data, settings)
    # Save the ICC colour profile before we clear img.info below.
    icc_profile = img.info.get("icc_profile")
    source_family = _color_family(img.mode)

    img = _resize(_normalize_mode(img), params, settings)
    out_format = params.format or _default_output_format(source.format)
    # Prepare the pixel mode for the chosen encoder (e.g. JPEG can't store alpha).
    img = _prepare_for_encoder(img, out_format)

    # Strip ALL metadata: EXIF (including GPS), XMP, text chunks, GIF comments.
    # ICC profiles describe the colour space; they're harmless to keep — but only if the
    # colour family hasn't changed (e.g. converting CMYK to RGB invalidates the profile).
    img.info = {}
    if source_family == "other" or _color_family(img.mode) != source_family:
        icc_profile = None

    buf = io.BytesIO()
    img.save(buf, OUTPUT_FORMATS[out_format], **_save_options(out_format, params.quality, icc_profile))
    return ProcessedImage(buf.getvalue(), out_format, MIME_TYPES[out_format], img.width, img.height, source)


def _decode(data: bytes, settings: Settings) -> tuple[Image.Image, SourceInfo]:
    """Open with only our allowed decoders, check the pixel count from the header, then decode upright."""
    try:
        # formats= restricts Pillow to only the decoders we explicitly allow (no BMP, TIFF, etc.).
        source = Image.open(io.BytesIO(data), formats=INPUT_FORMATS)
    except UnidentifiedImageError:
        supported = ", ".join(f.lower() for f in INPUT_FORMATS)
        raise ApiError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            f"The URL did not return a supported image. Supported input formats: {supported}.",
        ) from None
    except Image.DecompressionBombError:  # Pillow's own hard limit, if it is lower than ours
        raise _too_many_pixels("The source image has more pixels than the decoder allows.") from None
    except DECODE_ERRORS:
        raise _corrupt() from None

    with source:
        # Image.open() is lazy — it only reads the header. Check pixel count before decoding
        # the full image to prevent memory exhaustion attacks (e.g. a 100k×100k PNG).
        width, height = source.size
        if width * height > settings.max_input_pixels:
            raise _too_many_pixels(
                f"Source image is {width}x{height} ({width * height:,} pixels); "
                f"the limit is {settings.max_input_pixels:,} pixels."
            )
        try:
            source.load()  # decode the first frame only (animated GIFs etc. are truncated here)
            # Rotate the image according to its EXIF orientation tag so a photo taken sideways
            # comes out the right way up regardless of the output format.
            img = ImageOps.exif_transpose(source)
        except DECODE_ERRORS:
            raise _corrupt() from None
        source_format = (source.format or "").lower()
    return img, SourceInfo(source_format, img.width, img.height, len(data))


def _default_output_format(source_format: str) -> OutputFormat:
    for name in OUTPUT_FORMATS:
        if name == source_format:
            return name
    return "png"  # the source format can't be written (e.g. BMP): PNG is lossless and keeps alpha


def _prepare_for_encoder(img: Image.Image, out_format: OutputFormat) -> Image.Image:
    if out_format == "jpeg" and img.mode in ("RGBA", "LA"):
        return _flatten(img)
    if out_format in RGB_ONLY_FORMATS and img.mode in ("L", "LA"):
        # These encoders would convert grey to RGB internally, after the ICC check; do it here instead.
        return img.convert("RGBA" if img.mode == "LA" else "RGB")
    return img


def target_size(src: tuple[int, int], params: ProcessParams, max_dim: int) -> tuple[int, int]:
    sw, sh = src
    w, h = params.width, params.height
    if w is not None and h is not None:
        if params.crop == "fit":
            ratio = min(w / sw, h / sh)
            return max(1, round(sw * ratio)), max(1, round(sh * ratio))
        return w, h  # scale and fill both produce exactly w x h
    if w is not None:
        size = (w, max(1, round(sh * w / sw)))
    elif h is not None:
        size = (max(1, round(sw * h / sh)), h)
    else:
        return src
    if max(size) > max_dim:
        raise ApiError(
            ErrorCode.OUTPUT_TOO_LARGE,
            f"Keeping the aspect ratio would produce a {size[0]}x{size[1]} image; "
            f"the maximum is {max_dim} per side. Pass both width and height.",
        )
    return size


def _resize(img: Image.Image, params: ProcessParams, settings: Settings) -> Image.Image:
    size = target_size(img.size, params, settings.max_output_dimension)
    if size == img.size:
        return img
    if params.crop == "fill":
        return ImageOps.fit(img, size, RESAMPLE)  # cover, then centre-crop
    return img.resize(size, RESAMPLE, reducing_gap=REDUCING_GAP)


def _corrupt() -> ApiError:
    return ApiError(
        ErrorCode.CORRUPT_IMAGE, "The source image could not be decoded; it may be truncated or corrupt."
    )


def _too_many_pixels(message: str) -> ApiError:
    return ApiError(ErrorCode.SOURCE_TOO_MANY_PIXELS, message)


def _color_family(mode: str) -> ColorFamily:
    if mode in ("1", "L", "LA", "I", "F") or mode.startswith("I;16"):
        return "gray"
    if mode in ("RGB", "RGBA", "P", "PA"):
        return "rgb"
    return "other"  # CMYK, YCbCr, LAB...


def _normalize_mode(img: Image.Image) -> Image.Image:
    """Bring everything into L, LA, RGB, or RGBA so resizing and encoding behave predictably."""
    if img.mode in ("L", "LA", "RGB", "RGBA"):
        return img
    if img.mode == "1":
        return img.convert("L")
    if img.mode.startswith("I;16"):
        return img.convert("I").point(lambda v: v / SIXTEEN_TO_EIGHT_BITS).convert("L")
    if img.mode in ("I", "F"):
        return img.convert("L")
    has_alpha = "A" in img.mode or "transparency" in img.info
    return img.convert("RGBA" if has_alpha else "RGB")


def _flatten(img: Image.Image) -> Image.Image:
    """JPEG has no alpha channel: composite onto white."""
    rgba = img.convert("RGBA")
    background = Image.new("RGB", rgba.size, WHITE)
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _save_options(fmt: OutputFormat, quality: int, icc_profile: bytes | None) -> dict[str, object]:
    options: dict[str, object] = {}
    if fmt in LOSSY_FORMATS:
        options["quality"] = quality
    if fmt == "jpeg":
        options.update(optimize=True, progressive=True)
    elif fmt == "webp":
        options["method"] = WEBP_METHOD
    elif fmt == "png":
        options["compress_level"] = PNG_COMPRESS_LEVEL
    if icc_profile and fmt != "gif":
        options["icc_profile"] = icc_profile
    return options
