"""Colour: each clip balanced (exposure, white balance, levels), then one look for the film.

Everything is a 3D LUT (.cube, 33 points a side): a clip's LUT is its balance followed by the
look, so the renderer applies one `lut3d` per shot, and Premiere can take the same look on an
adjustment layer (Lumetri > Creative > Look). Footage is Rec.709 (not Log), so corrections stay
small: at most ±0.3 EV and a ±8 % channel gain. Clips are matched to the film's own colour
(the median of all its clips), not to grey: a golden hall stays golden, the drone that saw
the same place greener moves towards it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

CUBE_SIZE = 33
EV_MAX = 0.3
GAIN_MAX = 0.08
CAST_SHARE = 0.7  # how much of the difference to the film's colour is taken out
WHITE_TO = 0.96  # the 99th percentile goes here
BLACK_MAX = 0.08  # raised blacks above this are left alone (fog, a bright room)
LOOK_NAME = "T-Studio Тёплое кино"
_LUMA = np.array([0.2126, 0.7152, 0.0722])


@dataclass(frozen=True)
class ClipGrade:
    """out = (in - black) * gain[c] * exposure, per channel."""

    black: float
    exposure: float  # a factor, 2**EV
    gain: tuple[float, float, float]

    @staticmethod
    def neutral() -> ClipGrade:
        return ClipGrade(0.0, 1.0, (1.0, 1.0, 1.0))


def _midtones(frames: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    px = np.concatenate([f.reshape(-1, 3) for f in frames]).astype(np.float64)
    luma = px @ _LUMA
    return px, px[(luma > 0.15) & (luma < 0.85) & (px.max(axis=1) < 0.97)]


def chroma(frames: list[np.ndarray]) -> np.ndarray | None:
    """A clip's colour: the mean of its midtones divided by their brightness (r, g, b)."""
    if not frames:
        return None
    _, mid = _midtones(frames)
    if len(mid) < 50:
        return None
    means = mid.mean(axis=0)
    return means / max(float(means @ _LUMA), 1e-3)


def film_chroma(clips: list[np.ndarray | None]) -> np.ndarray | None:
    """The colour every clip of the film is matched to: the median of the clips' colours, so
    the place keeps its light (gold, a warm hall) and only the odd camera moves."""
    known = [c for c in clips if c is not None]
    return np.median(np.stack(known), axis=0) if known else None


def balance(frames: list[np.ndarray], target: np.ndarray | None = None) -> ClipGrade:
    """The correction for a clip from a few of its frames (H x W x 3, floats 0..1): levels and
    exposure from its brightness, white balance towards `target` (the film's colour)."""
    if not frames:
        return ClipGrade.neutral()
    px, _ = _midtones(frames)
    luma = px @ _LUMA
    black = float(np.percentile(luma, 1))
    black = black * 0.8 if black <= BLACK_MAX else 0.0
    white = float(np.percentile(luma, 99))
    exposure = float(np.clip(WHITE_TO / max(white - black, 1e-3), 2**-EV_MAX, 2**EV_MAX))
    own = chroma(frames)
    if own is None or target is None:
        return ClipGrade(black, exposure, (1.0, 1.0, 1.0))
    full = target / np.maximum(own, 1e-3)
    gain = np.clip(1.0 + (full - 1.0) * CAST_SHARE, 1 - GAIN_MAX, 1 + GAIN_MAX)
    gain = gain / float(gain @ _LUMA)  # white balance must not change the exposure
    r, g, b = (float(x) for x in gain)
    return ClipGrade(black, exposure, (r, g, b))


def apply_balance(rgb: np.ndarray, grade: ClipGrade) -> np.ndarray:
    gain = np.asarray(grade.gain) * grade.exposure
    return np.clip((rgb - grade.black) * gain, 0.0, 1.0)


def look(rgb: np.ndarray) -> np.ndarray:
    """«Тёплое кино»: soft contrast, lifted blacks, warm midtones and skin, cool deep shadows,
    clean whites (the dress stays white), richer colour in the middle, calmer in highlights."""
    x = np.clip(rgb, 0.0, 1.0)
    x = x - 0.32 * np.sin(2 * np.pi * x) / (2 * np.pi)  # S-curve per channel
    x = 0.03 + x * (0.97 - 0.03)  # film black and a soft white
    y = x @ _LUMA
    mid = (4 * y * (1 - y))[..., None]  # 1 in the midtones, 0 at black and white
    shadow = ((1 - y) ** 3)[..., None]
    warm = np.array([0.045, 0.012, -0.035]) * mid + np.array([-0.012, 0.004, 0.02]) * shadow
    x = x + warm
    y = (x @ _LUMA)[..., None]
    sat = 1.0 + 0.14 * mid - 0.35 * np.clip((y - 0.82) / 0.18, 0, 1)
    out: np.ndarray = np.clip(y + (x - y) * sat, 0.0, 1.0)
    return out


def _grid() -> np.ndarray:
    v = np.linspace(0.0, 1.0, CUBE_SIZE)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")  # .cube order: red changes fastest
    return np.stack([r, g, b], axis=-1).reshape(-1, 3)


def cube_text(grade: ClipGrade | None = None, *, with_look: bool = True, title: str = "") -> str:
    rgb = _grid()
    if grade is not None:
        rgb = apply_balance(rgb, grade)
    if with_look:
        rgb = look(rgb)
    head = [f'TITLE "{title or LOOK_NAME}"', f"LUT_3D_SIZE {CUBE_SIZE}",
            "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]  # fmt: skip
    rows = (f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in rgb)
    return "\n".join([*head, *rows]) + "\n"


def write_cube(path: Path, grade: ClipGrade | None = None, *, with_look: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(cube_text(grade, with_look=with_look), encoding="utf-8")
    tmp.replace(path)
    return path
