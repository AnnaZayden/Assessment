"""Grab one frame from an untrusted video with ffmpeg.

ffmpeg parses hundreds of formats, and some (HLS playlists, concat lists) can make it open other
files or URLs. So it only ever reads the one local file we wrote, only with common containers,
inside a time limit, and on Linux with memory and CPU caps.

Why imageio-ffmpeg instead of a system ffmpeg?
It ships its own ffmpeg binary as a Python package so there's no system dependency to install,
no version mismatch between dev and production, and it works identically in Docker and locally.
"""

from __future__ import annotations

import asyncio
import logging
import resource
import sys
import tempfile
from pathlib import Path
from typing import Final

import imageio_ffmpeg

from .config import Settings
from .errors import ApiError, ErrorCode

log = logging.getLogger(__name__)

FFMPEG: Final = imageio_ffmpeg.get_ffmpeg_exe()
CONTAINERS: Final = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi"
SUPPORTED: Final = "MP4, MOV, WebM, MKV, AVI"
MAX_FRAME_SIDE: Final = 4096  # bigger frames are shrunk by ffmpeg before they reach Pillow
MEMORY_LIMIT_BYTES: Final = 1024 * 1024 * 1024


def ffmpeg_args(path: Path, time_s: float) -> list[str]:
    return [
        FFMPEG,
        "-nostdin",           # don't try to read from stdin (we pass DEVNULL anyway)
        "-hide_banner",       # suppress version/build info from stderr
        "-loglevel", "error", # only print actual errors, not progress
        "-protocol_whitelist", "file",       # only allow reading local files (blocks http://, hls://, etc.)
        "-format_whitelist", CONTAINERS,     # only allow the containers we listed above
        "-ss", f"{time_s:g}", # seek to this timestamp before decoding (fast: seeks before decode)
        "-i", str(path),      # input file
        "-map", "0:v:0",      # take only the first video stream; ignore audio, subtitles, etc.
        "-frames:v", "1",     # extract exactly one frame then stop
        # Shrink frames larger than MAX_FRAME_SIDE on either side before they reach Pillow.
        # Keeps memory usage predictable for 4K+ source videos.
        "-vf", f"scale=w='min(iw,{MAX_FRAME_SIDE})':h='min(ih,{MAX_FRAME_SIDE})'"
        ":force_original_aspect_ratio=decrease",
        "-threads", "1",      # single-threaded: we cap concurrency at the semaphore level, not per-process
        "-f", "image2pipe",   # output format: raw image piped to stdout
        "-c:v", "png",        # encode the frame as PNG (lossless; Pillow will re-encode it)
        "-",                  # write to stdout
    ]  # fmt: skip


def _limit_resources(cpu_seconds: int) -> None:  # pragma: no cover - runs in the ffmpeg child process
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))


async def extract_frame(data: bytes, time_s: float, settings: Settings) -> bytes:
    """Extract the frame at ``time_s`` seconds from the video, returned as PNG bytes."""
    timeout = settings.video_timeout_s
    cpu_seconds = int(timeout) + 1
    with tempfile.TemporaryDirectory(prefix="thumb-") as tmp:
        path = Path(tmp) / "source"
        # Write the video bytes to a temp file. ffmpeg reads from a file, never from stdin,
        # so we can enforce -protocol_whitelist file to block any network access.
        await asyncio.to_thread(path.write_bytes, data)
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_args(path, time_s),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,  # frame PNG bytes come back here
            stderr=asyncio.subprocess.PIPE,  # ffmpeg error messages come back here
            # Apply OS-level resource limits inside the child process before it starts.
            # setrlimit(RLIMIT_AS) isn't supported on macOS, so the caps only apply on Linux.
            preexec_fn=(lambda: _limit_resources(cpu_seconds)) if sys.platform == "linux" else None,
        )
        try:
            frame, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            raise ApiError(
                ErrorCode.PROCESSING_TIMEOUT, f"Reading a frame from the video took longer than {timeout:g}s."
            ) from None
        finally:
            # Kill ffmpeg if it's still running (covers both timeout and client disconnection).
            # Without this, a hung ffmpeg process would linger indefinitely.
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    if proc.returncode != 0:
        log.debug("ffmpeg failed code=%s stderr=%r", proc.returncode, stderr[-500:])
        raise ApiError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            f"The URL did not return a readable video. Supported formats: {SUPPORTED}.",
        )
    if not frame:
        # ffmpeg exits 0 but writes no bytes when the seek time is past the end of the video.
        raise ApiError(
            ErrorCode.INVALID_PARAMETER,
            f"The video has no frame at {time_s:g}s; it is probably shorter than that.",
            param="time",
        )
    return frame
