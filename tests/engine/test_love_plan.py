"""Love story from the AI plan: beats, checks, repeats, slow motion, fitting to the length."""

from dataclasses import replace
from itertools import accumulate, pairwise

from toystudio.core.ai_plan import AiPlan, AiShot
from toystudio.engine.audio.beats import Beats
from toystudio.engine.edit.day import DayClip, MusicTrack
from toystudio.engine.edit.love import build_from_plan as _build


def build_from_plan(*a, **k):
    """The story as planned (the teaser and dynamics have tests of their own)."""
    k.setdefault("lively", False)
    return _build(*a, **k)


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


def shot(clip, beats=3, pct=30.0, part="development", why="руки", slow=False):
    return AiShot(clip, pct, beats, part, why, slow)


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
    plan = _plan(shot(1, 2, pct=30), shot(2, 2), shot(1, 2, pct=30))
    a, _, b = build_from_plan(plan, _clips(), _track(), 88 * S).shots
    assert a.media_id == b.media_id == 1
    assert b.src_in >= a.src_out or b.src_out <= a.src_in


def test_fitted_to_the_target_length():
    long_plan = _plan(*(shot(1 + k, 4) for k in range(60)))  # 60 x 2.4 s = 144 s
    d = build_from_plan(long_plan, _clips(60, seconds=30), _track(), 88 * S)
    assert d.stats.duration_us == 88 * S
    short_plan = _plan(shot(1, 2), shot(2, 2))
    d2 = build_from_plan(short_plan, _clips(seconds=200), _track(), 30 * S)
    assert d2.stats.duration_us == 30 * S  # the last shot runs on


def test_too_long_plan_keeps_its_climax_and_ending():
    middle = [shot(1 + k, 4) for k in range(40)]  # 40 x 2.4 s: far more than 30 s
    plan = _plan(*middle, shot(41, 1, part="climax"), shot(42, 4, part="outro"))
    d = build_from_plan(plan, _clips(42, seconds=30), _track(), 30 * S)
    assert [s.part for s in d.shots[-2:]] == ["climax", "outro"]
    assert d.shots[-1].media_id == 42 and d.stats.duration_us == 30 * S


# --- repeats a viewer notices


def test_climax_keeps_four_jump_cuts_of_one_take():
    burst = [shot(5, 1, pct=10 + 8 * k, part="climax") for k in range(10)]
    d = build_from_plan(_plan(shot(1), *burst, shot(2, part="outro")), _clips(seconds=60),
                        _track(), 88 * S)  # fmt: skip
    assert [s.media_id for s in d.shots] == [1, 5, 5, 5, 5, 2]


def test_a_clip_is_used_at_most_twice():
    plan = _plan(*(shot(c) for c in (1, 2, 1, 3, 1, 4)))
    ids = [s.media_id for s in build_from_plan(plan, _clips(), _track(), 88 * S).shots]
    assert ids.count(1) == 2


def test_look_alike_shots_are_not_side_by_side():
    looks = {1: 0, 2: 0b111, 3: 2**64 - 1, 4: 2**40 - 1}  # 1 and 2 differ in 3 bits of 64
    plan = _plan(shot(1), shot(2), shot(3), shot(4))
    ids = [s.media_id for s in build_from_plan(plan, _clips(), _track(), 88 * S, looks).shots]
    assert ids == [1, 3, 2, 4]  # 2 waited until a different picture came between
    alone = _plan(shot(1), shot(1, pct=60), shot(2, part="outro"))
    ids = [s.media_id for s in build_from_plan(alone, _clips(), _track(), 88 * S).shots]
    assert ids == [1, 2]  # the same clip twice in a row: nowhere to move it, dropped


# --- slow motion and transitions


def _fps(clips, fps):
    return [replace(c, fps=fps) for c in clips]


def test_slow_shot_of_50_fps_footage_takes_half_the_source():
    d = build_from_plan(_plan(shot(1, 4, slow=True), shot(2, 4)), _fps(_clips(), 50.0),
                        _track(), 88 * S)  # fmt: skip
    slow, normal = d.shots[0], d.shots[1]
    assert slow.speed == 0.5 and slow.src_out - slow.src_in == 2 * BEAT
    assert slow.length == 4 * BEAT and normal.speed == 1.0
    assert d.timeline.video[0].speed == 0.5 and d.timeline.duration() == d.stats.duration_us


def test_slow_is_ignored_without_the_frames_and_in_the_climax():
    drone = build_from_plan(_plan(shot(1, 4, slow=True)), _fps(_clips(), 29.97), _track(), 88 * S)
    assert drone.shots[0].speed == 1.0
    jump = _plan(shot(2), shot(1, 1, slow=True, part="climax"))
    assert build_from_plan(jump, _fps(_clips(), 50.0), _track(), 88 * S).shots[1].speed == 1.0


def test_dissolves_out_of_the_intro_and_into_the_outro():
    plan = _plan(shot(1, 4, part="intro"), shot(2), shot(3, 1, part="climax"),
                 shot(4, 1, part="climax"), shot(5, part="outro"))  # fmt: skip
    d = build_from_plan(plan, _clips(), _track(), 88 * S)
    cuts = list(accumulate(s.length for s in d.shots))[:-1]
    assert d.fade and d.dissolves == (cuts[0], cuts[3])  # the climax stays sharp


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


def test_take_heads_and_tails_are_trimmed():
    from toystudio.engine.edit.day import HEAD_US, TAIL_US

    early = build_from_plan(_plan(shot(1, 2, pct=5.0)), _clips(), _track(), 88 * S)
    assert early.shots[0].src_in >= HEAD_US  # not the second the couple gets into the pose
    late = build_from_plan(_plan(shot(2, 2, pct=100.0)), _clips(), _track(), 88 * S)
    assert late.shots[0].src_out <= 8 * S - TAIL_US  # nor the moment the pose breaks
    short = build_from_plan(_plan(shot(3, 2, pct=0.0)), _clips(seconds=1.5), _track(), 88 * S)
    s = short.shots[0]  # a 1.5 s take: no room to trim both, the window sits inside it
    assert 0 < s.src_in and s.src_out < 1.5 * S


# --- the AI's check of the cut


def _rshot(i, clip, *labels):
    from pathlib import Path

    from toystudio.core.ai_plan import ReviewOption, ReviewShot

    opts = [ReviewOption("A", clip, 30.0, 1.0, Path("a.jpg"))]
    for label in labels:
        other = clip + 100 if label == "C" else clip
        opts.append(ReviewOption(label, other, 60.0, 0.5 if label == "C" else 1.0, Path("x.jpg")))
    return ReviewShot(i, 0, 3, "development", f"план {i}", tuple(opts))


def test_revised_plan_swaps_drops_and_keeps():
    from toystudio.core.ai_plan import AiReview, ReviewChange
    from toystudio.engine.edit.love import revised_plan

    shots = [_rshot(1, 1, "B", "C"), _rshot(2, 2, "B", "C"), _rshot(3, 3, "B"), _rshot(4, 4)]
    review = AiReview((ReviewChange(1, "B", "щурится"), ReviewChange(2, "C", "смаз"),
                       ReviewChange(3, "drop", "спиной"), ReviewChange(4, "C", "нет варианта")))  # fmt: skip
    plan = revised_plan(_plan(), shots, review)
    got = [(s.clip_id, s.start_pct, s.slow) for s in plan.shots]
    assert got == [(1, 60.0, False), (102, 60.0, True), (4, 30.0, False)]
    assert plan.story == _plan().story
    unchanged = revised_plan(_plan(), shots, AiReview(()))
    assert [s.clip_id for s in unchanged.shots] == [1, 2, 3, 4]


# --- the teaser and the dynamics


def _story():
    """A calm story of ~88 s: an aerial intro, 32 development shots of 4 beats (takes 2-18,
    two of them slow), two climax beats and an aerial outro."""
    shots = [shot(1, 4, part="intro")]
    shots += [shot(2 + k % 17, 4, pct=20.0 + 40 * (k >= 17), slow=k in (3, 8)) for k in range(32)]
    shots += [
        shot(20, 1, part="climax"),
        shot(20, 1, pct=60, part="climax"),
        shot(21, 6, part="outro"),
    ]
    return _plan(*shots)


def _story_clips(static=(), aerial=(1, 5, 21)):
    clips = _fps(_clips(21, seconds=20), 50.0)
    return [replace(c, has_audio=c.media_id not in aerial, static=c.media_id in static)
            for c in clips]  # fmt: skip


def test_teaser_flashes_the_best_moments_first_and_keeps_the_length():
    from toystudio.engine.edit.day import TEASER_FLASHES

    d = _build(_story(), _story_clips(), _track(), 88 * S)
    teaser = [s for s in d.shots if s.part == "teaser"]
    story = [s for s in d.shots if s.part != "teaser"]
    assert 5 <= len(teaser) <= TEASER_FLASHES and d.shots[: len(teaser)] == tuple(teaser)
    assert all(s.length == BEAT for s in teaser)
    assert len({s.media_id for s in teaser}) == len(teaser)  # each take once
    for t in teaser:  # another moment of the take than the one in the story
        for s in story:
            if s.media_id == t.media_id:
                assert t.src_out <= s.src_in or s.src_out <= t.src_in
    slow_takes = {s.media_id for s in story if s.speed < 1}
    assert slow_takes <= {s.media_id for s in teaser}  # the emotional moments lead it
    assert d.stats.duration_us == 88 * S
    assert [p for p, k in d.styles if k == "white"] == [len(teaser) * BEAT]  # flash to white


def test_still_frames_move_aerials_fly_faster_and_calm_runs_get_a_quick_pair():
    from toystudio.engine.edit.day import DRONE_FAST, DRONE_FAST_MAX

    d = _build(_story(), _story_clips(static=(3, 4, 6, 7)), _track(), 88 * S, with_teaser=False)
    moved = [s for s in d.shots if s.move]
    assert moved and {s.media_id for s in moved} <= {3, 4, 6, 7}
    still = [s for s in d.shots if s.media_id in {3, 4, 6, 7} and s.length >= 3 * BEAT]
    assert len(moved) == (len(still) + 1) // 2  # every other still frame
    assert [s.move for s in moved][:4] == ["in", "out", "in", "turn"]
    assert all(s.length >= 3 * BEAT for s in moved)
    fast = [s for s in d.shots if s.speed == DRONE_FAST]
    assert fast and len(fast) <= DRONE_FAST_MAX and all(s.media_id == 5 for s in fast)
    assert all(s.src_out - s.src_in == round(s.length * DRONE_FAST) for s in fast)
    pairs = [k for k, s in enumerate(d.shots) if s.reason.code == "day_pace"]
    assert pairs  # a quick pair: the same take, two moments, a whip between
    for k in pairs:
        a, b = d.shots[k - 1], d.shots[k]
        assert a.media_id == b.media_id and a.length + b.length == 4 * BEAT
    whips = [p for p, kind in d.styles if kind == "whip"]
    assert len(whips) == len(pairs)
    assert d.stats.duration_us == 88 * S


def test_a_frozen_cut_rebuilds_the_same_without_a_second_teaser():
    from toystudio.core.ai_plan import AiShot

    first = _build(_story(), _story_clips(static=(3,)), _track(), 88 * S)
    frozen = replace(_story(), shots=tuple(
        AiShot(s.media_id, 100 * s.src_in / (20 * S), round(s.length / BEAT), s.part
               if s.part != "development" else "development", s.reason.ru, s.speed < 1)
        for s in first.shots), revised=True)  # fmt: skip
    again = _build(frozen, _story_clips(static=(3,)), _track(), 88 * S)
    assert sum(s.part == "teaser" for s in again.shots) == sum(
        s.part == "teaser" for s in first.shots
    )
    assert len(again.shots) == len(first.shots)  # no second split, nothing dropped
    assert [k for _, k in again.styles] == [k for _, k in first.styles]  # white, whips kept


def test_music_sections_follow_the_cut():
    from toystudio.engine.edit.day import music_sections

    d = _build(_story(), _story_clips(static=(3,)), _track(), 88 * S)
    secs = music_sections(d)
    moods = [s.mood for s in secs]
    assert moods[:3] == ["teaser", "intro", "calm"] and moods[-1] == "outro" and "climax" in moods
    assert secs[0].start_us == 0 and secs[-1].end_us == d.stats.duration_us
    assert all(a.end_us == b.start_us for a, b in pairwise(secs))
    middle = [s for s in secs if s.mood in ("calm", "tender", "pace")]
    assert all(s.end_us - s.start_us >= 4 * S for s in middle[1:])  # music cannot turn faster

    slow_run = _plan(*(shot(2 + k, 4, slow=True) for k in range(4)),
                     *(shot(9 + k, 4) for k in range(8)))  # fmt: skip
    moods = [s.mood for s in music_sections(_build(slow_run, _story_clips(), _track(), 88 * S,
                                                   with_teaser=False))]  # fmt: skip
    assert moods[0] == "tender"  # four slow shots in a row: a tender stretch


def test_music_written_to_the_cut_starts_with_it():
    from toystudio.engine.edit.day import Shot, _music_under

    fitted = MusicTrack(9, 95 * S, Beats(92.0, tuple(range(S, 95 * S, 652_000)), 0.9),
                        "sde", True)  # fmt: skip
    r = shot(1).why
    from toystudio.core.models import Reason

    (piece,) = _music_under([Shot(1, 0, 88 * S, False, Reason("x", r, r), 0)], {0: fitted})
    assert piece.src_in == S  # from its first heard beat, not aligned to its ending
