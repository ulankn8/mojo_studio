"""Media probing and integrity checks via ffprobe/ffmpeg."""

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from toystudio.core.models import US_PER_S, MediaInfo
from toystudio.engine.media.ffmpeg import find_tool, popen_flags

HDR_TRCS = {"arib-std-b67", "smpte2084"}


def rotation_of(stream: dict[str, Any]) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            return round(float(sd["rotation"]))
    tag = (stream.get("tags") or {}).get("rotate")
    return int(tag) if tag else 0


def _fps(text: str | None) -> float:
    try:
        f = Fraction(text or "0")
    except (ValueError, ZeroDivisionError):
        return 0.0
    return float(f)


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def probe_media(path: Path) -> MediaInfo:
    proc = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True,
        check=False,
        creationflags=popen_flags(),
    )  # fmt: skip
    if proc.returncode != 0:
        raise ValueError(f"ffprobe failed for {path}: {proc.stderr.decode(errors='replace')}")
    data = json.loads(proc.stdout)
    streams: list[dict[str, Any]] = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if video is None and not has_audio:
        raise ValueError(f"no audio or video streams in {path}")
    fmt = data.get("format", {})
    tags = fmt.get("tags") or {}
    duration = float(fmt.get("duration") or (video or {}).get("duration") or 0)
    width = height = rotation = 0
    fps = 0.0
    codec = ""
    hdr = False
    if video is not None:
        rotation = rotation_of(video)
        width, height = _int(video.get("width")), _int(video.get("height"))
        if abs(rotation) % 180 == 90:
            width, height = height, width
        fps = _fps(video.get("avg_frame_rate"))
        codec = str(video.get("codec_name", ""))
        hdr = video.get("color_transfer") in HDR_TRCS
    else:
        audio = next(s for s in streams if s.get("codec_type") == "audio")
        codec = str(audio.get("codec_name", ""))
    return MediaInfo(
        duration_us=round(duration * US_PER_S),
        width=width,
        height=height,
        rotation=rotation,
        fps=fps,
        codec=codec,
        hdr=hdr,
        has_audio=has_audio,
        has_video=video is not None,
        bit_rate=_int(fmt.get("bit_rate")),
        creation_time=tags.get("creation_time"),
    )


def _decode_errors(args: list[str]) -> bool:
    """True if decoding to null succeeds with no error output."""
    proc = subprocess.run(
        [find_tool("ffmpeg"), "-v", "error", *args, "-f", "null", "-"],
        capture_output=True,
        check=False,
        creationflags=popen_flags(),
    )
    return proc.returncode == 0 and not proc.stderr.strip()


def check_integrity(path: Path, edge_s: float = 2.0) -> bool:
    """Decode the first and last ``edge_s`` seconds; False on any decode error."""
    src = str(path)
    head = _decode_errors(["-i", src, "-t", f"{edge_s:.3f}"])
    if not head:
        return False
    return _decode_errors(["-sseof", f"-{edge_s:.3f}", "-i", src])


def audio_channels(path: Path) -> int:
    """Channels of the first audio stream; 0 when there is none or ffprobe cannot tell."""
    proc = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        creationflags=popen_flags(),
    )  # fmt: skip
    return _int(proc.stdout.strip().splitlines()[0]) if proc.stdout.strip() else 0
