"""Love story from the AI plan: beats, checks, fitting to the length."""

from toystudio.core.ai_plan import AiPlan, AiShot
from toystudio.engine.audio.beats import Beats
from toystudio.engine.edit.day import DayClip, MusicTrack
from toystudio.engine.edit.love import build_from_plan

S = 1_000_000
BEAT = 600_000  # 100 BPM


def _clips(n: int = 10, seconds: float = 8, bad=None):
    return [
        DayClip(
            i + 1,
            "стаб",
            i * 60 * S,
            round(seconds * S),
            True,
            (),
            (),
            tuple(bad.get(i + 1, ())) if bad else (),
        )
        for i in range(n)
    ]


def _track():
    beats = tuple(range(200_000, 240 * S, BEAT))
    return MusicTrack(99, 240 * S, Beats(100.0, beats, 0.9))


def _plan(*shots):
    return AiPlan("Утро, встреча, прогулка и крыша", tuple(shots))


def shot(clip, beats=3, pct=30.0, part="development", why="руки"):
    return AiShot(clip, pct, beats, part, why)


def test_plan_becomes_beat_long_shots_in_its_order():
    d = build_from_plan(_plan(shot(3, 2), shot(1, 3), shot(5, 1)), _clips(), _track(), 88 * S)
    assert [s.media_id for s in d.shots] == [3, 1, 5]
    lengths = [s.src_out - s.src_in for s in d.shots]
    assert lengths[:2] == [2 * BEAT, 3 * BEAT]
    assert d.shots[0].src_in == round(0.3 * 8 * S)
    assert d.timeline.video[1].reasons[0].code == "ai_love"
    assert d.timeline.video[1].reasons[0].ru == "руки"


def test_unknown_clip_is_dropped_and_out_of_range_start_moved_inside():
    d = build_from_plan(_plan(shot(42), shot(2, 4, pct=100.0)), _clips(), _track(), 88 * S)
    (s,) = d.shots
    assert s.media_id == 2 and s.src_out <= 8 * S and s.src_out - s.src_in >= 4 * BEAT


def test_bad_frames_are_avoided():
    clips = _clips(bad={1: [(0, 5 * S)]})
    d = build_from_plan(_plan(shot(1, 2, pct=10.0)), clips, _track(), 88 * S)
    (s,) = d.shots
    assert s.src_in >= 5 * S - BEAT  # moved off the shaky start


def test_same_window_twice_moves_on():
    d = build_from_plan(_plan(shot(1, 2, pct=30), shot(1, 2, pct=30)), _clips(), _track(), 88 * S)
    a, b = d.shots
    assert b.src_in >= a.src_out or b.src_out <= a.src_in


def test_fitted_to_the_target_length():
    long_plan = _plan(*(shot(1 + k % 10, 4) for k in range(60)))  # 60 x 2.4 s = 144 s
    d = build_from_plan(long_plan, _clips(seconds=30), _track(), 88 * S)
    assert d.stats.duration_us == 88 * S
    short_plan = _plan(shot(1, 2), shot(2, 2))
    d2 = build_from_plan(short_plan, _clips(seconds=200), _track(), 30 * S)
    assert d2.stats.duration_us == 30 * S  # the last shot runs on


def test_music_under_the_clip_and_empty_plan():
    d = build_from_plan(_plan(shot(1), shot(2)), _clips(), _track(), 88 * S)
    assert d.timeline.music and d.timeline.music[0].media_id == "99"
    assert build_from_plan(_plan(), _clips(), _track(), 88 * S).shots == ()


def test_plan_in_fractions_is_read_as_percent():
    fractions = _plan(shot(1, 2, pct=0.3), shot(2, 2, pct=0.6))
    percent = _plan(shot(1, 2, pct=30.0), shot(2, 2, pct=60.0))
    a = build_from_plan(fractions, _clips(), _track(), 88 * S)
    b = build_from_plan(percent, _clips(), _track(), 88 * S)
    assert [s.src_in for s in a.shots] == [s.src_in for s in b.shots]
    assert a.shots[0].src_in == round(0.3 * 8 * S)
