"""One ffmpeg decode -> proxy 540p + wav 16 kHz mono + 160x90 gray frames in a pipe.

Python reads the raw frames from ffmpeg's stdout in bounded chunks and computes the frame
metrics. `-progress` goes to stderr and is parsed in a thread. Every ffmpeg call is an
argument list (no shell), so Cyrillic and space-containing paths are fine on Windows.
Outputs are written to ``*.part`` files and renamed only when everything succeeded.
"""

import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

import numpy as np

from toystudio.core.models import US_PER_S, MediaInfo
from toystudio.engine.analysis.envelope import envelope_from_wav
from toystudio.engine.analysis.metrics import FrameReader, MetricsAccumulator
from toystudio.engine.media.ffmpeg import find_tool, popen_flags

PROXY_SHORT_SIDE = 540
METRIC_LONG, METRIC_SHORT = 160, 90
METRIC_FPS = 4.0
POLL_S = 0.1
PROGRESS_EVERY_S = 0.25
PROGRESS_KEYS = {
    "frame", "fps", "bitrate", "total_size", "out_time_ms", "out_time",
    "dup_frames", "drop_frames", "speed", "progress",
}  # fmt: skip
STANDARD_FPS = {
    "24000/1001": 24000 / 1001,
    "24": 24.0,
    "25": 25.0,
    "30000/1001": 30000 / 1001,
    "30": 30.0,
    "50": 50.0,
    "60000/1001": 60000 / 1001,
    "60": 60.0,
}

# zimg path: scale first (cheap), then linear light -> tonemap -> BT.709.
ZSCALE_TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)
# No zimg: treat the HLG signal as BT.709 gamma and only convert primaries / matrix.
FALLBACK_TONEMAP = "colorspace=all=bt709:iall=bt2020:fast=1,format=yuv420p"
SDR_TAGS = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"


class Stopped(Exception):
    """The caller asked to stop; ffmpeg was killed and no output remains."""


class FfmpegFailed(Exception):
    def __init__(self, message: str, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


@dataclass(frozen=True)
class Outputs:
    proxy: Path
    wav: Path
    frames: Path
    envelope: Path


def outputs_for(cache: Path, media_id: int) -> Outputs:
    return Outputs(
        proxy=cache / "proxy" / f"{media_id}.mp4",
        wav=cache / "audio" / f"{media_id}.wav",
        frames=cache / "frames" / f"{media_id}.npz",
        envelope=cache / "wave" / f"{media_id}.npy",
    )


def part_of(path: Path) -> Path:
    return path.with_name(path.name + ".part")


def pick_cfr(fps: float) -> str:
    """Nearest standard frame rate (as an ffmpeg fraction) for a possibly VFR source."""
    if fps <= 0:
        return "25"
    return min(STANDARD_FPS, key=lambda k: abs(STANDARD_FPS[k] - fps))


def proxy_size_filter(width: int, height: int, short_side: int = PROXY_SHORT_SIDE) -> str:
    """Scale so the SHORT side of the displayed picture is 540 (portrait 4K -> 540x960)."""
    if width >= height:
        return f"scale=-2:{short_side}"
    return f"scale={short_side}:-2"


def metric_size(width: int, height: int) -> tuple[int, int]:
    """(w, h) of the gray metric frames: 160x90, or 90x160 for portrait pictures."""
    return (METRIC_LONG, METRIC_SHORT) if width >= height else (METRIC_SHORT, METRIC_LONG)


def build_filter_graph(info: MediaInfo, *, tonemap: Literal["zscale", "fallback"]) -> str:
    chain = [proxy_size_filter(info.width, info.height)]
    if info.hdr:
        chain.append(ZSCALE_TONEMAP if tonemap == "zscale" else FALLBACK_TONEMAP)
        chain.append(SDR_TAGS)
    chain.append(f"fps=fps={pick_cfr(info.fps)}")
    chain.append("format=yuv420p")
    mw, mh = metric_size(info.width, info.height)
    tail = f"fps=fps={METRIC_FPS:g},scale={mw}:{mh}:flags=area,format=gray"
    return f"[0:v:0]{','.join(chain)},split=2[proxy][t];[t]{tail}[m]"


def build_command(
    src: Path,
    info: MediaInfo,
    out: Outputs,
    *,
    tonemap: Literal["zscale", "fallback"],
) -> list[str]:
    graph = build_filter_graph(info, tonemap=tonemap)
    gop = max(round(STANDARD_FPS[pick_cfr(info.fps)]), 1)  # 1 s
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error"]
    cmd += ["-progress", "pipe:2", "-y", "-i", str(src), "-filter_complex", graph]
    cmd += ["-map", "[proxy]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]
    cmd += ["-g", str(gop), "-pix_fmt", "yuv420p", "-color_primaries", "bt709"]
    cmd += ["-color_trc", "bt709", "-colorspace", "bt709", "-movflags", "+faststart"]
    cmd += ["-metadata:s:v", "rotate=0"]
    if info.has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-ar", "48000", "-b:a", "96k"]
    cmd += ["-f", "mp4", str(part_of(out.proxy))]
    if info.has_audio:
        cmd += ["-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
        cmd += ["-f", "wav", str(part_of(out.wav))]
    cmd += ["-map", "[m]", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    return cmd


def _parts(out: Outputs) -> list[Path]:
    return [part_of(p) for p in (out.proxy, out.wav, out.frames, out.envelope)]


def _cleanup(out: Outputs) -> None:
    for p in _parts(out):
        p.unlink(missing_ok=True)


def _tonemap() -> Literal["zscale", "fallback"]:
    from toystudio.engine.media.ffmpeg import has_filter

    return "zscale" if has_filter("zscale") else "fallback"


class _Progress:
    """Parses ffmpeg's -progress lines from stderr in a thread."""

    def __init__(self, stream: IO[bytes]) -> None:
        self.out_time_us = 0
        self.tail: deque[str] = deque(maxlen=30)
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream: IO[bytes]) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            key, sep, value = line.partition("=")
            if sep and key and " " not in key:
                if key == "out_time_us":
                    try:
                        self.out_time_us = max(self.out_time_us, int(value))
                    except ValueError:
                        pass
                if key in PROGRESS_KEYS or key.startswith("stream_") or key == "out_time_us":
                    continue
            if line:
                self.tail.append(line)

    def join(self) -> None:
        self._thread.join(timeout=2)


def _pump(
    reader: FrameReader, q: "queue.Queue[np.ndarray | None]", errors: list[BaseException]
) -> None:
    try:
        for chunk in reader.chunks():
            q.put(chunk)
    except (OSError, ValueError) as e:
        errors.append(e)
    finally:
        q.put(None)


def run(
    src: Path,
    info: MediaInfo,
    out: Outputs,
    *,
    on_progress: Callable[[float], None],
    should_stop: Callable[[], bool],
) -> dict[str, np.ndarray]:
    if not info.has_video:
        raise ValueError("single pass needs a video stream")
    for p in (out.proxy, out.wav, out.frames, out.envelope):
        p.parent.mkdir(parents=True, exist_ok=True)
    _cleanup(out)
    duration_s = info.duration_us / US_PER_S
    cmd = build_command(src, info, out, tonemap=_tonemap())
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=popen_flags(),
    )
    assert proc.stdout is not None and proc.stderr is not None
    progress = _Progress(proc.stderr)
    mw, mh = metric_size(info.width, info.height)
    q: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=8)
    pump_errors: list[BaseException] = []
    pump = threading.Thread(
        target=_pump, args=(FrameReader(proc.stdout, mw, mh), q, pump_errors), daemon=True
    )
    pump.start()
    acc = MetricsAccumulator(fps=METRIC_FPS)
    try:
        last_emit = float("-inf")
        done_s = 0.0
        while True:
            if should_stop():
                raise Stopped()
            try:
                chunk = q.get(timeout=POLL_S)
            except queue.Empty:
                chunk = np.empty((0, mh, mw), dtype=np.uint8)
            else:
                if chunk is None:
                    break
                acc.update(chunk)
            now = time.monotonic()
            cur = min(progress.out_time_us / US_PER_S, duration_s)
            if cur > done_s and now - last_emit >= PROGRESS_EVERY_S:
                done_s, last_emit = cur, now
                on_progress(done_s)
        if pump_errors:
            raise FfmpegFailed(f"frame pipe failed: {pump_errors[0]}", "\n".join(progress.tail))
        while True:  # moov rewrite (+faststart) can take a while: stay stoppable
            if should_stop():
                raise Stopped()
            try:
                rc = proc.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
        progress.join()
        if rc != 0:
            tail = "\n".join(progress.tail)
            raise FfmpegFailed(f"ffmpeg exited with code {rc}", tail)
        result = acc.result()
        _finalize(out, info, result)
        on_progress(duration_s)
        return result
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        while pump.is_alive():  # unblock the pump if the queue is full
            try:
                q.get(timeout=0.05)
            except queue.Empty:
                pass
        progress.join()
        _cleanup(out)
        raise


def run_audio(
    src: Path,
    info: MediaInfo,
    out: Outputs,
    *,
    on_progress: Callable[[float], None],
    should_stop: Callable[[], bool],
) -> None:
    """Audio-only file (speech from the sound desk): wav 16 kHz mono + envelope, no proxy."""
    if not info.has_audio:
        raise ValueError("audio pass needs an audio stream")
    for p in (out.wav, out.envelope):
        p.parent.mkdir(parents=True, exist_ok=True)
    _cleanup(out)
    duration_s = info.duration_us / US_PER_S
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error"]
    cmd += ["-progress", "pipe:2", "-y", "-i", str(src), "-map", "0:a:0"]
    cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(part_of(out.wav))]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=popen_flags(),
    )
    assert proc.stderr is not None
    progress = _Progress(proc.stderr)
    try:
        last_emit = float("-inf")
        while True:
            if should_stop():
                raise Stopped()
            try:
                rc = proc.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if now - last_emit >= PROGRESS_EVERY_S:
                last_emit = now
                on_progress(min(progress.out_time_us / US_PER_S, duration_s))
        progress.join()
        if rc != 0:
            raise FfmpegFailed(f"ffmpeg exited with code {rc}", "\n".join(progress.tail))
        env_part = part_of(out.envelope)
        with env_part.open("wb") as f:
            np.save(f, envelope_from_wav(part_of(out.wav)))
        os.replace(env_part, out.envelope)
        os.replace(part_of(out.wav), out.wav)  # wav last: it marks the pass as complete
        on_progress(duration_s)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        progress.join()
        _cleanup(out)
        raise


def _finalize(out: Outputs, info: MediaInfo, metrics: dict[str, np.ndarray]) -> None:
    frames_part = part_of(out.frames)
    with frames_part.open("wb") as f:
        np.savez(f, **metrics)  # type: ignore[arg-type]
    os.replace(frames_part, out.frames)
    if info.has_audio:
        env_part = part_of(out.envelope)
        env = envelope_from_wav(part_of(out.wav))
        with env_part.open("wb") as f:
            np.save(f, env)
        os.replace(env_part, out.envelope)
        os.replace(part_of(out.wav), out.wav)
    os.replace(part_of(out.proxy), out.proxy)
