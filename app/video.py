"""Grab one frame from an untrusted video with ffmpeg.

ffmpeg parses hundreds of formats, and some (HLS playlists, concat lists) can make it open other
files or URLs. So it only ever reads the one local file we wrote, only with common containers,
inside a time limit, and on Linux with memory and CPU caps.
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
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-protocol_whitelist", "file",
        "-format_whitelist", CONTAINERS,
        "-ss", f"{time_s:g}",
        "-i", str(path),
        "-map", "0:v:0",
        "-frames:v", "1",
        "-vf", f"scale=w='min(iw,{MAX_FRAME_SIDE})':h='min(ih,{MAX_FRAME_SIDE})'"
        ":force_original_aspect_ratio=decrease",
        "-threads", "1",
        "-f", "image2pipe",
        "-c:v", "png",
        "-",
    ]  # fmt: skip


def _limit_resources(cpu_seconds: int) -> None:  # pragma: no cover - runs in the ffmpeg child process
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))


async def extract_frame(data: bytes, time_s: float, settings: Settings) -> bytes:
    """The frame at ``time_s`` seconds, as PNG bytes."""
    timeout = settings.video_timeout_s
    cpu_seconds = int(timeout) + 1
    with tempfile.TemporaryDirectory(prefix="thumb-") as tmp:
        path = Path(tmp) / "source"
        await asyncio.to_thread(path.write_bytes, data)
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_args(path, time_s),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
            # Also covers cancellation: a client that disconnects must not leave ffmpeg running.
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
        raise ApiError(
            ErrorCode.INVALID_PARAMETER,
            f"The video has no frame at {time_s:g}s; it is probably shorter than that.",
            param="time",
        )
    return frame
