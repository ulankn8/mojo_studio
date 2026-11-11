"""Still frames for project cards and media lists."""

import os
import subprocess
from pathlib import Path

import numpy as np

from toystudio.core.models import US_PER_S
from toystudio.engine.media.ffmpeg import find_tool, popen_flags
from toystudio.engine.media.probe import probe_media


def card_thumbnail(proxy: Path, out: Path) -> Path:
    """JPEG of the frame in the middle of `proxy`, written via ``*.part`` -> ``os.replace``."""
    return frame_at(proxy, probe_media(proxy).duration_us // 2, out)


def frame_at(proxy: Path, at_us: int, out: Path, width: int | None = None) -> Path:
    """JPEG of the frame of `proxy` at `at_us` (scaled to `width`), via ``*.part`` -> replace."""
    at_s = max(at_us, 0) / US_PER_S
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-ss", f"{at_s:.3f}", "-i", str(proxy), "-frames:v", "1",
           *(["-vf", f"scale={width}:-2"] if width else []),
           "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", str(part)]  # fmt: skip
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=popen_flags(),
        )
        if proc.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            tail = proc.stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(f"thumbnail failed for {proxy} at {at_s:.3f} s: {tail}")
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
    return out


def average_hash(image: Path) -> int:
    """64-bit average hash of a still (8x8 grey, a bit per pixel brighter than the mean): two
    pictures of the same scene differ in a few bits, different scenes in about half."""
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-i", str(image), "-vf", "scale=8:8:flags=area,format=gray",
           "-frames:v", "1", "-f", "rawvideo", "-"]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, check=False, stdin=subprocess.DEVNULL,
                          creationflags=popen_flags())  # fmt: skip
    pixels = proc.stdout
    if proc.returncode != 0 or len(pixels) != 64:
        raise RuntimeError(f"hash failed for {image}")
    mean = sum(pixels) / 64
    return sum(1 << k for k, v in enumerate(pixels) if v > mean)


def small_rgb(image: Path, width: int = 96) -> np.ndarray:
    """A still as floats 0..1 (H x W x 3) at `width`, for colour statistics."""
    height = max(2, round(width * 9 / 16))
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-i", str(image), "-vf", f"scale={width}:{height}:flags=area,format=rgb24",
           "-frames:v", "1", "-f", "rawvideo", "-"]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, check=False, stdin=subprocess.DEVNULL,
                          creationflags=popen_flags())  # fmt: skip
    if proc.returncode != 0 or len(proc.stdout) != width * height * 3:
        raise RuntimeError(f"cannot read {image}")
    return np.frombuffer(proc.stdout, np.uint8).reshape(height, width, 3) / 255.0
