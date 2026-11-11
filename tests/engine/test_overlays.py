"""Light overlays of a clip: where an editor would put them, on beats, never overlapping."""

from itertools import pairwise

from toystudio.core.models import Reason
from toystudio.core.timeline import Timeline
from toystudio.engine.edit.day import DayDraft, Shot
from toystudio.engine.edit.overlays import ASSET_DIR, LINES_MAX, OVERLAYS, asset, place
from toystudio.engine.edit.restaurant import DraftStats

S = 1_000_000
BEAT = 600_000
R = Reason("x", "x", "x")


def _draft(parts, *, dissolves=(), fade=True, slow=()):
    def shot(k, part):
        slow_ = k in slow
        return Shot(1, 0, S + S // 2 if slow_ else 3 * S, False, R, 0, 0.5 if slow_ else 1.0, part)

    shots = tuple(shot(k, p) for k, p in enumerate(parts))
    total = sum(s.length for s in shots)
    return DayDraft(Timeline(), shots, ("love",), DraftStats(total, 0, 0, 1), tuple(dissolves),
                    fade, BEAT)  # fmt: skip


def _names(items):
    return [next(n for n, (i, _) in OVERLAYS.items() if i == it.media_id) for it in items]


def test_leak_sweep_bokeh_waves_and_lines_in_their_places():
    parts = ["intro"] + ["development"] * 20 + ["climax"] * 2 + ["outro"] * 2  # 75 s
    d = _draft(parts, dissolves=[3 * S, 45 * S, 69 * S], slow={12})
    items = place(d)
    names = _names(items)
    assert names[0] == "leak" and items[0].start == 0
    sweep = items[names.index("sweep")]
    assert sweep.start == 63 * S  # exactly on the climax's first cut
    assert "waves" in names and "bokeh" in names
    for it in items:
        assert it.track == "V2" and it.blend == "screen" and 0 < it.opacity <= 1
        if _names([it]) != ["sweep"]:
            assert it.start % BEAT == 0  # on the beat
    ordered = sorted(items, key=lambda i: i.start)
    assert all(a.end <= b.start for a, b in pairwise(ordered))


def test_lines_are_few_and_spaced():
    d = _draft(["development"] * 30, dissolves=[k * 6 * S for k in range(1, 15)])
    lines = [i for i in place(d) if _names([i]) == ["lines"]]
    assert len(lines) <= LINES_MAX
    assert all(b.start - a.start >= 15 * S - BEAT for a, b in pairwise(lines))


def test_film_and_empty_drafts_get_none():
    assert place(_draft(["development"] * 5, fade=False)) == []
    assert place(_draft([])) == []


def test_assets_exist_for_every_overlay():
    for name, (mid, _) in OVERLAYS.items():
        assert asset(mid) == ASSET_DIR / f"{name}.mp4" and asset(mid).is_file()
