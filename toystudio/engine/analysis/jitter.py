"""Hand shake at the full frame rate: how far the picture jitters around its smooth path.

The analysis samples 4 frames a second, too slow to see a hand trembling (several times a
second) and blind to the difference between a pan and a shake. Here the proxy is decoded at
its own frame rate, the camera path is the running sum of frame-to-frame phase-correlation
shifts, and jitter is the distance from that path to its own moving average: a smooth pan
follows its average, a trembling hand does not.
"""

from __future__ import annotations

import subprocess
from itertools import pairwise
from pathlib import Path

import numpy as np

from toystudio.core.models import US_PER_S
from toystudio.engine.analysis.metrics import phase_shift_xy
from toystudio.engine.media.ffmpeg import find_tool, popen_flags

WIDTH, HEIGHT = 320, 180
SMOOTH_S = 0.25  # the path a steady operator would follow
WINDOW_S = 0.5  # jitter averaged over this to call a stretch shaky
JITTER_PX = 1.2  # at 320 px wide; C0170 (hand shake) ~2.1, steadicam ~0.3, tripod 0
MIN_SHAKY_S = 0.5
STATIC_PX = 0.2  # median motion per frame below this: the camera stood still
MERGE_S = 1.0  # shaky stretches this close are one (the hand did not calm down between)
PAD_S = 0.2


def jitter(video: Path, fps: float) -> np.ndarray:
    """Per-frame jitter (px at 320 wide) of the whole `video` decoded at `fps`."""
    return measure(video, fps)[0]


def measure(video: Path, fps: float) -> tuple[np.ndarray, np.ndarray]:
    """(jitter, motion) per frame of `video` at `fps`: the shake, and how far the picture
    moved since the frame before (px at 320 wide; a tripod is 0, a slow pan ~0.6)."""
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
           "-i", str(video), "-an", "-vf", f"fps={fps:g},scale={WIDTH}:{HEIGHT},format=gray",
           "-f", "rawvideo", "-"]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, check=False, stdin=subprocess.DEVNULL,
                          creationflags=popen_flags())  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-500:])
    frames = np.frombuffer(proc.stdout, np.uint8).reshape(-1, HEIGHT, WIDTH)
    steps = _steps(frames)
    motion = np.concatenate([[0.0], np.hypot(*steps.T)]) if len(frames) else np.zeros(0)
    return _jitter(steps, fps), motion


def _steps(frames: np.ndarray) -> np.ndarray:
    """(dx, dy) of the picture from each frame to the next, by phase correlation."""
    return np.array([phase_shift_xy(a, b) for a, b in pairwise(frames)], dtype=np.float64).reshape(
        -1, 2
    )


def jitter_of(frames: np.ndarray, fps: float) -> np.ndarray:
    return _jitter(_steps(frames), fps) if len(frames) >= 3 else np.zeros(len(frames))


def _jitter(steps: np.ndarray, fps: float) -> np.ndarray:
    if len(steps) < 2:
        return np.zeros(len(steps) + 1)
    path = np.vstack([[0.0, 0.0], np.cumsum(steps, axis=0)])
    k = max(3, round(SMOOTH_S * fps) | 1)  # odd
    pad = np.pad(path, ((k // 2, k // 2), (0, 0)), mode="edge")
    kernel = np.ones(k) / k
    smooth = np.stack([np.convolve(pad[:, i], kernel, "valid") for i in range(2)], axis=1)
    out: np.ndarray = np.hypot(*(path - smooth).T)
    return out


def is_static(motion: np.ndarray) -> bool:
    """A locked-off camera: the picture barely moves (a tripod, a still pose on a gimbal)."""
    return bool(len(motion)) and float(np.median(motion)) < STATIC_PX


def shaky_spans(values: np.ndarray, fps: float) -> list[tuple[int, int]]:
    """(start_us, end_us) where jitter averaged over WINDOW_S is above JITTER_PX."""
    if len(values) == 0:
        return []
    k = max(1, round(WINDOW_S * fps))
    mean = np.convolve(values, np.ones(k) / k, "same")
    bad = mean > JITTER_PX
    runs: list[list[float]] = []
    start = None
    for i, b in enumerate([*bad, False]):
        if b and start is None:
            start = i
        elif not b and start is not None:
            a, e = start / fps - PAD_S, i / fps + PAD_S
            if runs and a - runs[-1][1] < MERGE_S:
                runs[-1][1] = e
            else:
                runs.append([a, e])
            start = None
    end = len(values) / fps
    return [(round(max(a, 0.0) * US_PER_S), round(min(e, end) * US_PER_S))
            for a, e in runs if e - a - 2 * PAD_S >= MIN_SHAKY_S]  # fmt: skip
