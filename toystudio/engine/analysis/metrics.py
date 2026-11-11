"""Frame metrics (stage 1b) and a bounded reader of raw gray frames from a pipe.

Pure functions on small numpy arrays; OpenCV is used for the Laplacian when present
with an equivalent numpy fallback.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO

import numpy as np

try:  # pragma: no cover - exercised implicitly where OpenCV is installed
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

METRIC_KEYS = ("sharpness", "brightness", "motion", "shake", "histdiff")
HIST_BINS = 32


def has_opencv() -> bool:
    return cv2 is not None


def laplacian_variance_np(frame: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian over the interior (numpy only)."""
    f = frame.astype(np.float32)
    lap = f[:-2, 1:-1] + f[2:, 1:-1] + f[1:-1, :-2] + f[1:-1, 2:] - 4.0 * f[1:-1, 1:-1]
    return float(lap.var())


def laplacian_variance(frame: np.ndarray) -> float:
    """Sharpness: variance of the Laplacian of one gray frame (uint8, h x w)."""
    if cv2 is not None:
        return float(cv2.Laplacian(frame, cv2.CV_32F).var())
    return laplacian_variance_np(frame)


def mean_brightness(frame: np.ndarray) -> float:
    """Mean luma scaled to 0..1."""
    return float(frame.mean()) / 255.0


def frame_motion(prev: np.ndarray, cur: np.ndarray) -> float:
    """Mean absolute difference of two gray frames scaled to 0..1."""
    return float(np.abs(cur.astype(np.int16) - prev.astype(np.int16)).mean()) / 255.0


def phase_shift_xy(prev: np.ndarray, cur: np.ndarray) -> tuple[int, int]:
    """Integer (dx, dy) of the picture between two gray frames, by phase correlation."""
    a = np.fft.fft2(prev.astype(np.float32))
    b = np.fft.fft2(cur.astype(np.float32))
    cross = a * np.conj(b)
    cross /= np.abs(cross) + 1e-9
    corr = np.fft.ifft2(cross).real
    iy, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
    h, w = corr.shape
    dy = iy - h if iy > h // 2 else iy
    dx = ix - w if ix > w // 2 else ix
    return int(dx), int(dy)


def phase_shift(prev: np.ndarray, cur: np.ndarray) -> float:
    """Camera shake: length (px) of the integer shift found by phase correlation."""
    dx, dy = phase_shift_xy(prev, cur)
    return float(np.hypot(dx, dy))


def luma_hist(frame: np.ndarray) -> np.ndarray:
    """Normalised 32-bin brightness histogram of a uint8 gray frame."""
    hist = np.bincount((frame >> 3).ravel(), minlength=HIST_BINS).astype(np.float64)
    return hist / max(hist.sum(), 1.0)


def hist_chi2(a: np.ndarray, b: np.ndarray) -> float:
    """Chi-square distance between two normalised histograms."""
    return float((((a - b) ** 2) / (a + b + 1e-9)).sum())


@dataclass
class MetricsAccumulator:
    """Feeds on chunks (n, h, w) of gray frames; keeps the last frame between chunks."""

    fps: float = 4.0
    _prev: np.ndarray | None = None
    _prev_hist: np.ndarray | None = None
    _cols: dict[str, list[float]] = field(default_factory=lambda: {k: [] for k in METRIC_KEYS})

    @property
    def frames(self) -> int:
        return len(self._cols["sharpness"])

    def update(self, chunk: np.ndarray) -> None:
        for frame in chunk:
            self._cols["sharpness"].append(laplacian_variance(frame))
            self._cols["brightness"].append(mean_brightness(frame))
            if self._prev is None:
                self._cols["motion"].append(0.0)
                self._cols["shake"].append(0.0)
            else:
                self._cols["motion"].append(frame_motion(self._prev, frame))
                self._cols["shake"].append(phase_shift(self._prev, frame))
            hist = luma_hist(frame)
            self._cols["histdiff"].append(
                0.0 if self._prev_hist is None else hist_chi2(self._prev_hist, hist)
            )
            self._prev = frame
            self._prev_hist = hist

    def result(self) -> dict[str, np.ndarray]:
        out = {k: np.asarray(v, dtype=np.float32) for k, v in self._cols.items()}
        out["t"] = np.arange(self.frames, dtype=np.float64) / self.fps
        return out


class FrameReader:
    """Reads fixed-size gray frames from a binary stream in bounded chunks.

    Memory is at most `chunk_frames * w * h` bytes (plus one copy), however many frames
    the stream holds. Short reads from a pipe are accumulated; a trailing partial frame
    is dropped and reported in `partial_bytes`.
    """

    def __init__(self, stream: BinaryIO, width: int, height: int, chunk_frames: int = 64) -> None:
        if width <= 0 or height <= 0 or chunk_frames <= 0:
            raise ValueError("width, height and chunk_frames must be positive")
        self.stream = stream
        self.width = width
        self.height = height
        self.chunk_frames = chunk_frames
        self.frame_bytes = width * height
        self.frames_read = 0
        self.partial_bytes = 0

    def chunks(self) -> Iterator[np.ndarray]:
        want = self.frame_bytes * self.chunk_frames
        buf = bytearray()
        while True:
            data = self.stream.read(want - len(buf))
            if not data:
                break
            buf += data
            if len(buf) >= want:
                yield self._emit(buf, want)
                del buf[:want]
        whole = (len(buf) // self.frame_bytes) * self.frame_bytes
        if whole:
            yield self._emit(buf, whole)
        self.partial_bytes = len(buf) - whole

    def _emit(self, buf: bytearray, nbytes: int) -> np.ndarray:
        arr = np.frombuffer(bytes(buf[:nbytes]), dtype=np.uint8)
        arr = arr.reshape(-1, self.height, self.width)
        self.frames_read += arr.shape[0]
        return arr
