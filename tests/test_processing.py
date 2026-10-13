"""Resizing, cropping, format conversion, colour handling and metadata stripping."""

from __future__ import annotations

import httpx
import pytest
from PIL import Image, ImageCms, PngImagePlugin

from app.formats import INPUT_FORMATS, OUTPUT_FORMATS

from .helpers import encode, error_of, make_image, open_image

# --- Happy paths -----------------------------------------------------------


def test_resize_fit_keeps_aspect_ratio(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(size=(800, 600)))
    r = client.get("/process", params={"url": "https://img.test/a.jpg", "width": 500, "height": 300})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert open_image(r.content).size == (400, 300)


def test_resize_fill_is_exact(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(size=(800, 600)))
    r = client.get(
        "/process", params={"url": "https://img.test/a.jpg", "width": 500, "height": 500, "crop": "fill"}
    )
    assert open_image(r.content).size == (500, 500)


def test_resize_scale_stretches(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(size=(800, 600)))
    r = client.get(
        "/process", params={"url": "https://img.test/a.jpg", "width": 100, "height": 400, "crop": "scale"}
    )
    assert open_image(r.content).size == (100, 400)


def test_single_dimension_is_proportional(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(size=(800, 600)))
    r = client.get("/process", params={"url": "https://img.test/a.jpg", "height": 150})
    assert open_image(r.content).size == (200, 150)


def test_convert_png_with_alpha_to_jpeg(client, upstream):
    upstream.image("img.test", "/a.png", make_image("PNG", mode="RGBA"), "image/png")
    r = client.get("/process", params={"url": "https://img.test/a.png", "format": "jpg", "quality": 60})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    img = open_image(r.content)
    assert img.format == "JPEG"
    assert img.mode == "RGB"


def test_combined_resize_and_webp(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(size=(1600, 1200)))
    r = client.get(
        "/process",
        params={
            "url": "https://img.test/a.jpg",
            "width": 800,
            "height": 600,
            "format": "webp",
            "crop": "fill",
        },
    )
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["content-disposition"] == 'inline; filename="image.webp"'
    assert open_image(r.content).size == (800, 600)


def test_source_details_are_reported(client, upstream):
    source = make_image("PNG", size=(1600, 1200))
    upstream.image("img.test", "/a.png", source, "image/png")
    r = client.get("/process", params={"url": "https://img.test/a.png", "width": 400, "format": "webp"})
    assert r.headers["x-source-format"] == "png"
    assert (r.headers["x-source-width"], r.headers["x-source-height"]) == ("1600", "1200")
    assert r.headers["x-source-bytes"] == str(len(source))
    assert (r.headers["x-image-width"], r.headers["x-image-height"]) == ("400", "300")


def test_metadata_is_stripped(client, upstream):
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"  # Make
    upstream.image("img.test", "/a.jpg", make_image(exif=exif.tobytes()))
    r = client.get("/process", params={"url": "https://img.test/a.jpg", "width": 100})
    assert "exif" not in open_image(r.content).info


def test_exif_orientation_is_applied(client, upstream):
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW
    upstream.image("img.test", "/a.jpg", make_image(size=(800, 600), exif=exif.tobytes()))
    r = client.get("/process", params={"url": "https://img.test/a.jpg"})
    assert open_image(r.content).size == (600, 800)


# --- Bad content / upstream errors ---------------------------------------------


def test_not_an_image(client, upstream):
    upstream.add("img.test", "/page", httpx.Response(200, text="<html></html>"))
    r = client.get("/process", params={"url": "https://img.test/page"})
    assert r.status_code == 415
    assert error_of(r)["code"] == "unsupported_media_type"


def test_rejects_tiff(client, upstream):
    upstream.image("img.test", "/a.tiff", make_image("TIFF"), "image/tiff")
    r = client.get("/process", params={"url": "https://img.test/a.tiff"})
    assert r.status_code == 415
    assert error_of(r)["code"] == "unsupported_media_type"


def test_corrupt_image(client, upstream):
    upstream.image("img.test", "/broken.jpg", make_image()[:400])
    r = client.get("/process", params={"url": "https://img.test/broken.jpg"})
    assert r.status_code == 422
    assert error_of(r)["code"] == "corrupt_image"


def test_upstream_404(client):
    r = client.get("/process", params={"url": "https://img.test/missing.jpg"})
    assert r.status_code == 502
    assert error_of(r)["upstream_status"] == 404


# --- Formats, colour modes, and pixels -----------------------------------------------


@pytest.mark.parametrize("fmt", [f for f in OUTPUT_FORMATS if f != "jpeg"])
def test_output_formats(client, upstream, fmt):
    upstream.image("img.test", "/a.jpg", make_image(size=(200, 100)))
    r = client.get("/process", params={"url": "https://img.test/a.jpg", "format": fmt, "width": 50})
    assert r.status_code == 200
    assert r.headers["content-type"] == f"image/{fmt}"
    img = open_image(r.content)
    assert img.format == OUTPUT_FORMATS[fmt]
    assert img.size == (50, 25)


@pytest.mark.parametrize("fmt", [f for f in INPUT_FORMATS if f not in ("JPEG", "PNG")])
def test_input_formats_decode(client, upstream, fmt):
    upstream.image("img.test", "/a", make_image(fmt, size=(200, 100)), "application/octet-stream")
    r = client.get("/process", params={"url": "https://img.test/a", "width": 100, "format": "png"})
    assert r.status_code == 200
    assert r.headers["x-source-format"] == fmt.lower()
    assert open_image(r.content).size == (100, 50)


def test_default_format_keeps_source(client, upstream):
    upstream.image("img.test", "/a.webp", make_image("WEBP"), "image/webp")
    r = client.get("/process", params={"url": "https://img.test/a.webp", "width": 10})
    assert r.headers["content-type"] == "image/webp"
    assert open_image(r.content).format == "WEBP"


def test_unwritable_source_format_falls_back_to_png(client, upstream):
    upstream.image("img.test", "/a.bmp", make_image("BMP"), "image/bmp")
    r = client.get("/process", params={"url": "https://img.test/a.bmp", "width": 10})
    assert r.headers["content-type"] == "image/png"
    assert open_image(r.content).format == "PNG"


def test_format_is_sniffed_not_taken_from_content_type(client, upstream):
    upstream.image("img.test", "/a", make_image("PNG"), "text/html")
    r = client.get("/process", params={"url": "https://img.test/a", "width": 10})
    assert r.status_code == 200
    assert r.headers["x-source-format"] == "png"


def test_svg_rejected(client, upstream):
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'
    )
    upstream.image("img.test", "/a.svg", svg, "image/svg+xml")
    r = client.get("/process", params={"url": "https://img.test/a.svg"})
    assert r.status_code == 415
    assert error_of(r)["code"] == "unsupported_media_type"


def test_animated_gif_outputs_first_frame(client, upstream):
    red, blue = Image.new("RGB", (40, 40), (255, 0, 0)), Image.new("RGB", (40, 40), (0, 0, 255))
    upstream.image("img.test", "/a.gif", encode(red, "GIF", save_all=True, append_images=[blue]), "image/gif")
    r = client.get("/process", params={"url": "https://img.test/a.gif"})
    img = open_image(r.content)
    assert img.format == "GIF"
    assert getattr(img, "n_frames", 1) == 1
    assert img.convert("RGB").getpixel((20, 20)) == (255, 0, 0)


@pytest.mark.parametrize(
    ("source", "fmt", "expected_mode"),
    [
        (Image.new("1", (20, 20), 1), "PNG", "L"),
        (Image.new("L", (20, 20), 128), "PNG", "L"),
        (Image.new("LA", (20, 20), (128, 255)), "PNG", "LA"),
        (Image.new("I;16", (20, 20), 40000), "PNG", "L"),
        (Image.new("CMYK", (20, 20), (0, 255, 255, 0)), "JPEG", "RGB"),
        (Image.new("P", (20, 20), 0), "GIF", "RGB"),
    ],
    ids=["1-bit", "gray", "gray-alpha", "16-bit", "cmyk", "palette"],
)
def test_colour_modes_are_normalised(client, upstream, source, fmt, expected_mode):
    upstream.image("img.test", "/a", encode(source, fmt))
    r = client.get("/process", params={"url": "https://img.test/a", "format": "png"})
    assert r.status_code == 200
    assert open_image(r.content).mode == expected_mode


def test_palette_with_transparency_keeps_alpha(client, upstream):
    source = Image.new("P", (20, 20), 0)
    upstream.image("img.test", "/a.png", encode(source, "PNG", transparency=0))
    r = client.get("/process", params={"url": "https://img.test/a.png", "format": "png"})
    img = open_image(r.content)
    assert img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0


def test_transparent_to_jpeg_is_flattened_onto_white(client, upstream):
    upstream.image("img.test", "/a.png", encode(Image.new("RGBA", (20, 20), (0, 0, 0, 0)), "PNG"))
    r = client.get("/process", params={"url": "https://img.test/a.png", "format": "jpeg"})
    assert all(channel > 250 for channel in open_image(r.content).getpixel((10, 10)))


def test_quality_changes_lossy_output(client, upstream):
    noisy = Image.effect_noise((300, 300), 64).convert("RGB")
    upstream.image("img.test", "/a.png", encode(noisy, "PNG"))
    sizes = {
        q: len(
            client.get(
                "/process", params={"url": "https://img.test/a.png", "format": "jpeg", "quality": q}
            ).content
        )
        for q in (10, 95)
    }
    assert sizes[10] < sizes[95] / 2


def test_quality_is_ignored_for_png(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(size=(100, 100)))
    low, high = (
        client.get(
            "/process", params={"url": "https://img.test/a.jpg", "format": "png", "quality": q}
        ).content
        for q in (10, 95)
    )
    assert low == high


def test_fill_crops_the_centre_but_scale_stretches(client, upstream):
    # 400x200: green strip on the far left, red middle-left, blue middle-right.
    source = Image.new("RGB", (400, 200), (255, 0, 0))
    source.paste((0, 0, 255), (200, 0, 400, 200))
    source.paste((0, 255, 0), (0, 0, 60, 200))
    upstream.image("img.test", "/a.png", encode(source, "PNG"))
    params = {"url": "https://img.test/a.png", "width": 100, "height": 100, "format": "png"}
    fill = open_image(client.get("/process", params={**params, "crop": "fill"}).content).convert("RGB")
    scale = open_image(client.get("/process", params={**params, "crop": "scale"}).content).convert("RGB")
    # fill keeps the middle 200x200: no green left, red/blue split in the centre.
    assert fill.getpixel((2, 50)) == (255, 0, 0)
    assert fill.getpixel((97, 50)) == (0, 0, 255)
    # scale squeezes the whole width in, so the green strip survives at the left edge.
    assert scale.getpixel((2, 50)) == (0, 255, 0)


def test_width_only_is_proportional(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(size=(800, 600)))
    r = client.get("/process", params={"url": "https://img.test/a.jpg", "width": 400})
    assert open_image(r.content).size == (400, 300)


def test_fit_can_enlarge(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(size=(100, 50)))
    r = client.get("/process", params={"url": "https://img.test/a.jpg", "width": 400, "height": 400})
    assert open_image(r.content).size == (400, 200)


def test_corrupt_pixel_data_after_valid_header(client, upstream):
    data = encode(Image.effect_noise((200, 200), 64), "PNG")
    upstream.image("img.test", "/a.png", data[: len(data) // 2])
    r = client.get("/process", params={"url": "https://img.test/a.png"})
    assert r.status_code == 422
    assert error_of(r)["code"] == "corrupt_image"


# --- Metadata and colour profiles -------------------------------------------------------


def _private_jpeg() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (51.0, 30.0, 0.0)
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><secret/></x:xmpmeta>'
    return make_image(size=(50, 50), exif=exif.tobytes(), xmp=xmp)


def _private_png() -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Secret Person")
    info.add_itxt("XML:com.adobe.xmp", "<secret/>")
    return encode(Image.new("RGB", (50, 50)), "PNG", pnginfo=info)


@pytest.mark.parametrize("source", [_private_jpeg(), _private_png()], ids=["jpeg-exif-gps-xmp", "png-text"])
@pytest.mark.parametrize("fmt", ["jpeg", "png", "webp"])
def test_all_metadata_is_stripped(client, upstream, source, fmt):
    upstream.image("img.test", "/a", source)
    r = client.get("/process", params={"url": "https://img.test/a", "format": fmt})
    img = open_image(r.content)
    assert not set(img.info) & {"exif", "xmp", "XML:com.adobe.xmp", "Author"}
    assert not getattr(img, "text", {})
    assert b"Secret" not in r.content
    assert b"secret" not in r.content


SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def test_icc_profile_kept_when_it_still_matches(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(icc_profile=SRGB))
    r = client.get("/process", params={"url": "https://img.test/a.jpg", "format": "png", "width": 10})
    assert open_image(r.content).info.get("icc_profile") == SRGB


CMYK_WITH_ICC = encode(Image.new("CMYK", (20, 20)), "JPEG", icc_profile=SRGB)
GRAY_WITH_ICC = encode(Image.new("LA", (20, 20)), "PNG", icc_profile=SRGB)


@pytest.mark.parametrize(
    ("source", "fmt"),
    [pytest.param(CMYK_WITH_ICC, fmt, id=f"cmyk-{fmt}") for fmt in OUTPUT_FORMATS if fmt != "gif"]
    + [
        pytest.param(GRAY_WITH_ICC, fmt, id=f"grey-{fmt}")
        for fmt in ("jpeg", "webp", "avif")
        if fmt in OUTPUT_FORMATS
    ],
)
def test_icc_profile_dropped_when_colour_space_changes(client, upstream, source, fmt):
    upstream.image("img.test", "/a", source)
    r = client.get("/process", params={"url": "https://img.test/a", "format": fmt})
    assert r.status_code == 200
    assert "icc_profile" not in open_image(r.content).info


def test_icc_profile_kept_for_grey_output(client, upstream):
    upstream.image("img.test", "/a.png", GRAY_WITH_ICC)
    r = client.get("/process", params={"url": "https://img.test/a.png", "format": "png"})
    img = open_image(r.content)
    assert img.mode == "LA"
    assert img.info.get("icc_profile") == SRGB


@pytest.mark.parametrize("fmt", ["gif", "jpeg", "png"])
def test_gif_comment_is_stripped(client, upstream, fmt):
    upstream.image("img.test", "/a.gif", encode(Image.new("P", (20, 20)), "GIF", comment=b"Secret comment"))
    r = client.get("/process", params={"url": "https://img.test/a.gif", "format": fmt})
    assert r.status_code == 200
    assert b"Secret" not in r.content
    assert "comment" not in open_image(r.content).info


def test_gif_output_never_embeds_icc(client, upstream):
    upstream.image("img.test", "/a.jpg", make_image(icc_profile=SRGB))
    r = client.get("/process", params={"url": "https://img.test/a.jpg", "format": "gif"})
    assert "icc_profile" not in open_image(r.content).info


def test_pillow_decompression_limit_maps_to_too_many_pixels(client, upstream, monkeypatch):
    # If Pillow's own hard limit is ever lower than ours, its error must still become a clean 422.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    upstream.image("img.test", "/a.png", make_image("PNG", size=(300, 300)))
    r = client.get("/process", params={"url": "https://img.test/a.png"})
    assert r.status_code == 422
    assert error_of(r)["code"] == "source_too_many_pixels"
