"""Colour: a clip matched to the film's colour, levels, and the warm look as a valid .cube."""

import numpy as np

from toystudio.engine.color.grade import (
    CUBE_SIZE,
    EV_MAX,
    GAIN_MAX,
    ClipGrade,
    apply_balance,
    balance,
    chroma,
    cube_text,
    film_chroma,
    look,
)


def _frame(rgb, spread=0.6, size=(36, 64)):
    """A gradient frame of one colour, from dark to bright."""
    ramp = np.linspace(0.05, 0.05 + spread, size[1])[None, :, None]
    return np.broadcast_to(ramp * np.asarray(rgb), (*size, 3)).copy()


def test_dark_cold_clip_gets_brighter_and_warmer_towards_the_film():
    warm = [_frame((1.05, 1.0, 0.9)) for _ in range(3)]
    cold = [_frame((0.9, 1.0, 1.12), spread=0.45)]
    target = film_chroma([chroma(f) for f in (warm, warm, cold)])
    g = balance(cold, target)
    assert g.exposure > 1.0
    assert g.gain[0] > 1.0 > g.gain[2]  # more red, less blue
    after = apply_balance(cold[0], g)
    assert chroma([after])[2] < chroma(cold)[2]


def test_corrections_are_limited_and_keep_exposure():
    g = balance([_frame((0.2, 1.0, 2.0), spread=0.1)], np.array([1.0, 1.0, 1.0]))
    assert 2**-EV_MAX - 1e-9 <= g.exposure <= 2**EV_MAX + 1e-9
    gains = np.asarray(g.gain)
    assert np.all(gains >= (1 - GAIN_MAX) * 0.9) and np.all(gains <= (1 + GAIN_MAX) * 1.1)
    assert abs(float(gains @ np.array([0.2126, 0.7152, 0.0722])) - 1.0) < 1e-9


def test_no_frames_or_no_target_is_safe():
    assert balance([]) == ClipGrade.neutral()
    g = balance([_frame((1.0, 1.0, 1.0))])
    assert g.gain == (1.0, 1.0, 1.0)
    assert film_chroma([None, None]) is None


def test_look_keeps_white_white_and_warms_the_midtones():
    white, grey = look(np.array([[1.0, 1.0, 1.0]]))[0], look(np.array([[0.5, 0.5, 0.5]]))[0]
    assert white.min() > 0.93 and white.max() - white.min() < 0.03  # the dress stays white
    assert grey[0] > grey[2] and abs(grey.mean() - 0.5) < 0.04  # warm, not brighter
    black = look(np.array([[0.0, 0.0, 0.0]]))[0]
    assert 0.01 < black.mean() < 0.06  # film black, not crushed


def test_cube_file_is_valid():
    text = cube_text(ClipGrade(0.02, 1.1, (1.02, 1.0, 0.97)))
    lines = text.strip().splitlines()
    assert lines[1] == f"LUT_3D_SIZE {CUBE_SIZE}"
    rows = [list(map(float, ln.split())) for ln in lines[4:]]
    assert len(rows) == CUBE_SIZE**3 and all(len(r) == 3 for r in rows)
    values = np.array(rows)
    assert values.min() >= 0.0 and values.max() <= 1.0
    plain = cube_text(with_look=False).strip().splitlines()[4:]
    assert plain[1] == f"{1 / (CUBE_SIZE - 1):.6f} 0.000000 0.000000"  # red changes fastest
