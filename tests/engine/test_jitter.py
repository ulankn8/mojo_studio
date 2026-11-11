"""Hand shake at the full frame rate: jitter around the smooth camera path."""

import numpy as np

from toystudio.engine.analysis.jitter import HEIGHT, WIDTH, jitter_of, shaky_spans

S = 1_000_000
FPS = 50.0
RNG = np.random.default_rng(7)
SCENE = (RNG.random((HEIGHT + 80, WIDTH + 80)) * 255).astype(np.uint8)


def _frames(offsets):
    """Crops of one textured scene at (dx, dy) per frame: a camera moving over it."""
    out = []
    for dx, dy in offsets:
        x, y = 40 + round(dx), 40 + round(dy)
        out.append(SCENE[y : y + HEIGHT, x : x + WIDTH])
    return np.stack(out)


def test_tripod_and_smooth_pan_are_steady():
    tripod = jitter_of(_frames([(0, 0)] * 50), FPS)
    pan = jitter_of(_frames([(k * 0.6, 0) for k in range(50)]), FPS)  # 30 px a second
    assert tripod.max() == 0.0
    assert pan.mean() < 0.5 and shaky_spans(pan, FPS) == []


def test_trembling_hand_is_found_where_it_trembles():
    calm = [(0, 0)] * 50
    shake = [tuple(RNG.integers(-4, 5, 2)) for _ in range(60)]  # 1.2 s of a trembling hand
    values = jitter_of(_frames(calm + shake + calm), FPS)
    ((a, b),) = shaky_spans(values, FPS)
    assert 0.6 * S <= a <= 1.1 * S and 2.1 * S <= b <= 2.6 * S


def test_close_shaky_stretches_merge_and_short_ones_drop():
    v = np.zeros(400)
    v[50:90] = 3.0  # 0.8 s
    v[110:150] = 3.0  # 0.4 s later: the same unsteady moment
    v[300:310] = 3.0  # 0.2 s: a bump, not a shaky shot
    spans = shaky_spans(v, FPS)
    assert len(spans) == 1 and spans[0][1] - spans[0][0] > 2 * S
    assert shaky_spans(np.zeros(0), FPS) == []


def test_tripod_is_static_and_a_pan_is_not():
    from toystudio.engine.analysis.jitter import _steps, is_static

    def motion(offsets):
        steps = _steps(_frames(offsets))
        return np.concatenate([[0.0], np.hypot(*steps.T)])

    assert is_static(motion([(0, 0)] * 30))
    assert not is_static(motion([(k * 0.6, 0) for k in range(30)]))
    assert not is_static(np.zeros(0))
