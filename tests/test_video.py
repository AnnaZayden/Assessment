"""/video/thumbnail: real ffmpeg on small generated videos, served by the fake upstream."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.video import FFMPEG

from .helpers import error_of, make_image, open_image

URL = "https://img.test/clip.mp4"


def make_video(tmp: Path, name: str, *args: str) -> bytes:
    """A 2-second, 10 fps, 320x240 test pattern whose frames all differ."""
    out = tmp / name
    subprocess.run(  # noqa: S603 - fixed arguments, our own ffmpeg
        [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", "2", *args, str(out)],
        check=True,
    )  # fmt: skip
    return out.read_bytes()


@pytest.fixture(scope="session")
def mp4(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return make_video(tmp_path_factory.mktemp("video"), "clip.mp4", "-pix_fmt", "yuv420p")


@pytest.fixture
def served(upstream, mp4):
    upstream.image("img.test", "/clip.mp4", mp4, "video/mp4")
    return upstream


def thumb(client, **params):
    return client.get("/video/thumbnail", params={"url": URL, **params})


def test_returns_the_first_frame_as_jpeg_by_default(client, served, mp4):
    r = thumb(client)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert open_image(r.content).size == (320, 240)
    assert r.headers["x-source-format"] == "video"
    assert r.headers["x-source-bytes"] == str(len(mp4))
    assert (r.headers["x-source-width"], r.headers["x-source-height"]) == ("320", "240")
    assert served.requests[0].headers["accept"] == "video/*"


def test_resizes_and_converts_like_process(client, served):
    r = thumb(client, width="100", format="webp")
    assert r.headers["content-type"] == "image/webp"
    assert open_image(r.content).size == (100, 75)

    r = thumb(client, width="50", height="50", crop="fill", format="png")
    assert open_image(r.content).size == (50, 50)


def test_format_auto_negotiates(client, served):
    r = client.get(
        "/video/thumbnail", params={"url": URL, "format": "auto"}, headers={"accept": "image/webp"}
    )
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["vary"] == "Accept"
    assert thumb(client, format="auto").headers["content-type"] == "image/jpeg"


def test_time_picks_the_frame(client, served):
    first = thumb(client, format="png").content
    assert thumb(client, time="0", format="png").content == first
    later = thumb(client, time="1.5", format="png")
    assert later.status_code == 200
    assert later.content != first


def test_time_past_the_end_is_a_parameter_error(client, served):
    r = thumb(client, time="30")
    assert r.status_code == 400
    assert error_of(r) == {
        "code": "invalid_parameter",
        "message": "The video has no frame at 30s; it is probably shorter than that.",
        "param": "time",
    }


@pytest.mark.parametrize("value", ["-1", "abc", "1e3", "1.2345", "86401", " ", "\u0661"])
def test_bad_time_values_are_rejected(client, value):
    r = thumb(client, time=value)
    assert r.status_code == 400
    error = error_of(r)
    assert error["param"] == "time"
    assert error["code"] == "invalid_parameter"


def test_time_is_only_a_video_parameter(client):
    r = client.get("/process", params={"url": URL, "time": "1"})
    assert error_of(r)["code"] == "unknown_parameter"
    r = thumb(client, tim="1")
    assert error_of(r)["message"] == (
        "Unknown query parameter 'tim'. Allowed parameters: url, width, height, format, quality, crop, time."
    )


def test_results_are_cached_per_time(client, served):
    assert thumb(client, time="1").headers["x-cache"] == "MISS"
    assert thumb(client, time="1").headers["x-cache"] == "HIT"
    assert thumb(client, time="1.5").headers["x-cache"] == "MISS"
    # An image URL processed by /process doesn't share the video's cache entry.
    assert client.get("/process", params={"url": URL}).status_code == 415


@pytest.mark.parametrize(
    "body",
    [
        make_image("JPEG"),  # an image, not a video: ffmpeg could read it, but not with our containers
        b"not a video at all",
        # A playlist that would make ffmpeg fetch another URL itself, bypassing our address checks.
        b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:9/a.ts\n#EXT-X-ENDLIST\n",
    ],
)
def test_non_videos_are_refused(client, upstream, body):
    upstream.image("img.test", "/clip.mp4", body, "video/mp4")
    r = thumb(client)
    assert r.status_code == 415
    assert error_of(r) == {
        "code": "unsupported_media_type",
        "message": "The URL did not return a readable video. Supported formats: MP4, MOV, WebM, MKV, AVI.",
    }


def test_other_containers_work(client, upstream, tmp_path):
    upstream.image("img.test", "/clip.avi", make_video(tmp_path, "clip.avi"), "video/x-msvideo")
    upstream.image("img.test", "/clip.mkv", make_video(tmp_path, "clip.mkv"), "video/x-matroska")
    for path in ("/clip.avi", "/clip.mkv"):
        r = client.get("/video/thumbnail", params={"url": f"https://img.test{path}", "width": "64"})
        assert r.status_code == 200, r.text
        assert open_image(r.content).size == (64, 48)


def test_videos_have_their_own_size_limit(make_client, served, mp4):
    client = make_client(max_video_bytes=len(mp4) - 1, max_source_bytes=len(mp4) * 10)
    r = thumb(client)
    assert r.status_code == 413
    assert error_of(r)["message"] == f"Source video is larger than the {len(mp4) - 1:,} bytes limit."

    client = make_client(max_video_bytes=len(mp4), max_source_bytes=1)
    assert thumb(client).status_code == 200


def test_slow_extraction_times_out(make_client, served):
    r = thumb(make_client(video_timeout_s=0.001))
    assert r.status_code == 422
    assert error_of(r) == {
        "code": "processing_timeout",
        "message": "Reading a frame from the video took longer than 0.001s.",
    }


def test_internal_urls_are_blocked(client, upstream):
    r = client.get("/video/thumbnail", params={"url": "http://internal.test/v.mp4"})
    assert r.status_code == 403
    assert error_of(r)["code"] == "url_not_allowed"
    assert upstream.requests == []


def test_is_rate_limited(make_client, served):
    client = make_client(rate_limit_burst=1)
    assert thumb(client).status_code == 200
    r = thumb(client)
    assert r.status_code == 429
    assert "retry-after" in r.headers


def test_is_documented(client):
    params = {
        p["name"]
        for p in client.get("/openapi.json").json()["paths"]["/video/thumbnail"]["get"]["parameters"]
    }
    assert params == {"url", "width", "height", "format", "quality", "crop", "time"}
