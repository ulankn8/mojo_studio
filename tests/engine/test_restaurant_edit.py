"""Restaurant auto-edit: blocks to keep, camera alternation, the reference sound under it."""

import itertools
import statistics

from toystudio.core.models import MediaRole, Track
from toystudio.engine.edit.restaurant import (
    DANCE_KEEP_US,
    Clip,
    RefAudio,
    build,
    find_blocks,
    intersect,
    layered,
    union,
)

S = 1_000_000
MAIN, CUT = MediaRole.MAIN, MediaRole.CUTAWAY


def _cams(length_s: int, *, cut_from: int = 0, cut_to: int | None = None, bad=()):
    """V1 (id 1) films the whole time, V2 (id 2) films [cut_from, cut_to)."""
    cut_to = length_s if cut_to is None else cut_to
    return [
        Clip(1, MAIN, 0, length_s * S),
        Clip(2, CUT, cut_from * S, (cut_to - cut_from) * S, tuple(bad)),
    ]


def _phrases(a: int, b: int, every: float = 4.0, gap: float = 0.4):
    """Speech from a to b seconds: phrases of `every` s with short breaths between them."""
    out, t = [], float(a)
    while t < b:
        out.append((round(t * S), round(min(t + every - gap, b) * S)))
        t += every
    return out


def _series(draft, clips):
    roles = {c.media_id: c.role for c in clips}
    return [(roles[s.media_id], (s.end - s.start) / S) for s in draft.shots]


def test_toast_is_kept_whole_and_cameras_alternate():
    clips = _cams(100)
    d = build(clips, [RefAudio(10, 0, 100 * S)], _phrases(20, 80), [])
    assert len(d.blocks) == 1
    b = d.blocks[0]
    assert b.start == 19 * S and b.end == 81 * S  # the toast with 1 s before and after
    series = _series(d, clips)
    roles = [r for r, _ in series]
    assert all(x is not y for x, y in itertools.pairwise(roles))  # strict alternation
    assert roles[0] is MAIN
    assert 4.5 <= statistics.median(x for r, x in series if r is MAIN) <= 6.5
    assert 3.5 <= statistics.median(x for r, x in series if r is CUT) <= 5.5
    assert sum(s.duration for s in d.timeline.video) == b.end - b.start
    assert sum(s.duration for s in d.timeline.speech) == b.end - b.start
    assert d.stats.cuts == len(d.shots) - 1 and d.stats.dropped_us == 100 * S - (b.end - b.start)


def test_cuts_land_on_phrase_boundaries():
    clips = _cams(100)
    speech = _phrases(10, 90, every=2.0)  # a breath at most 0.8 s away
    d = build(clips, [RefAudio(10, 0, 100 * S)], speech, [])
    bounds = {x for s, e in speech for x in (s, e)}
    inner = [s.start for s in d.shots[1:]]
    assert inner and sum(c in bounds for c in inner) / len(inner) >= 0.8


def test_long_pause_between_toasts_is_dropped_short_one_kept():
    clips = _cams(200)
    speech = _phrases(10, 40) + _phrases(46, 70) + _phrases(100, 130)  # 6 s and 30 s pauses
    d = build(clips, [RefAudio(10, 0, 200 * S)], speech, [])
    assert [(b.start // S, b.end // S, b.kind) for b in d.blocks] == [
        (9, 71, "talk"),
        (99, 131, "talk"),
    ]


def test_long_music_is_one_capped_take_of_the_cutaway_camera():
    clips = _cams(1000)
    music = [(100 * S, 820 * S)]  # 12 min of dancing
    d = build(clips, [RefAudio(10, 0, 1000 * S)], _phrases(20, 90), music)
    dance = [b for b in d.blocks if b.kind == "dance"]
    assert len(dance) == 1 and dance[0].end - dance[0].start == DANCE_KEEP_US
    takes = [s for s in d.shots if s.kind == "dance"]
    assert len(takes) == 1 and takes[0].media_id == 2  # one long take, cutaway camera


def test_short_music_inside_a_toast_stays():
    clips = _cams(200)
    speech = _phrases(10, 50) + _phrases(70, 110)
    d = build(clips, [RefAudio(10, 0, 200 * S)], speech, [(50 * S, 70 * S)])  # 20 s fanfare
    assert [(b.start // S, b.end // S) for b in d.blocks] == [(9, 111)]


def test_only_main_camera_where_the_cutaway_did_not_film():
    clips = _cams(100, cut_from=50)
    d = build(clips, [RefAudio(10, 0, 100 * S)], _phrases(10, 90), [])
    early = [s for s in d.shots if s.end <= 50 * S]
    assert early and all(s.media_id == 1 for s in early)
    assert any(s.media_id == 2 for s in d.shots if s.start >= 50 * S)


def test_bad_frames_give_the_slot_to_the_other_camera():
    bad = [(0, 100 * S)]  # the cutaway is shaky the whole time
    clips = _cams(100, bad=bad)
    d = build(clips, [RefAudio(10, 0, 100 * S)], _phrases(10, 90), [])
    assert {s.media_id for s in d.shots} == {1}
    assert len(d.shots) == 1  # merged into one long main shot


def test_speech_track_is_split_on_recorder_files():
    clips = _cams(100)
    refs = [RefAudio(10, 0, 50 * S), RefAudio(11, 50 * S, 50 * S)]
    d = build(clips, refs, _phrases(20, 80), [])
    pieces = [(s.media_id, s.src_in // S, s.src_out // S) for s in d.timeline.speech]
    assert pieces == [("10", 19, 50), ("11", 0, 31)]
    assert all(s.track is Track.SPEECH for s in d.timeline.speech)


def test_video_segments_are_in_clip_time():
    clips = [Clip(1, MAIN, 30 * S, 70 * S), Clip(2, CUT, 30 * S, 70 * S)]
    d = build(clips, [RefAudio(10, 0, 100 * S)], _phrases(40, 90), [])
    first = d.timeline.video[0]
    assert first.media_id == "1" and first.src_in == 9 * S  # R 39 s - offset 30 s
    assert all(0 <= s.src_in < s.src_out <= s.bound_out for s in d.timeline.video)


def test_nothing_to_edit():
    assert build([], [RefAudio(10, 0, 100 * S)], _phrases(10, 90), []).shots == ()
    no_sound = build(_cams(100), [], _phrases(10, 90), [])
    assert no_sound.shots == () and no_sound.timeline.video == ()
    quiet = build(_cams(100), [RefAudio(10, 0, 100 * S)], [], [])
    assert quiet.blocks == () and quiet.stats.dropped_us == 100 * S


def test_stray_word_in_a_pause_is_not_a_block():
    blocks = find_blocks([(0, 100 * S)], [(50 * S, 50 * S + 400_000)], [])
    assert blocks == []


def test_layered_keeps_main_under_inserts_and_sound_continuous():
    clips = _cams(100)
    d = build(clips, [RefAudio(10, 0, 100 * S)], _phrases(20, 80), [])
    items = layered(d, clips)
    v1 = [i for i in items if i.track == "V1"]
    v2 = [i for i in items if i.track == "V2"]
    audio = [i for i in items if i.track == "A"]
    total = d.stats.duration_us
    assert len(v1) == 1 and (v1[0].start, v1[0].end) == (0, total)  # one long main take
    assert v2 and all(i.media_id == 2 for i in v2)
    assert len(audio) == 1 and audio[0].end == total


def test_interval_helpers():
    assert union([(5, 7), (0, 2), (1, 3), (7, 9)]) == [(0, 3), (5, 9)]
    assert intersect([(0, 10), (20, 30)], [(5, 25)]) == [(5, 10), (20, 25)]
