"""Still frames for project cards and media lists."""

import os
import subprocess
from pathlib import Path

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
