"""The day before the restaurant: sections, rites, montage, love story, SDE and express."""

import statistics
from dataclasses import replace
from itertools import accumulate, pairwise

from toystudio.core.models import AudioMode, Reason, Track
from toystudio.engine.audio.beats import Beats
from toystudio.engine.edit.day import (
    JUMP_MAX,
    SLOW,
    TAKE_MAX_US,
    DayClip,
    MusicTrack,
    build_clip,
    build_film,
    day_layout,
    sections,
)

S = 1_000_000
MIN = 60 * S


class Day:
    """A synthetic wedding day: ids grow, times are minutes from the start of the day."""

    def __init__(self) -> None:
        self.clips: list[DayClip] = []

    def add(self, minute: float, seconds: float, *, live: bool = True, talk: float = 0.8,
            bad=()) -> DayClip:  # fmt: skip
        d = round(seconds * S)
        speech = ((0, round(d * talk)),) if live and talk else ()
        c = DayClip(len(self.clips) + 1, "стаб" if live else "дрон", round(minute * MIN), d,
                    live, speech, (), tuple(bad))  # fmt: skip
        self.clips.append(c)
        return c

    def montage(self, minute: float, n: int, seconds: float = 12, drone_every: int = 2) -> None:
        for k in range(n):
            self.add(minute + k * 0.5, seconds, live=k % drone_every != 0, talk=0)


def _day() -> Day:
    d = Day()
    d.add(0, 120)  # groom's house: two rites with a drone shot between them
    d.add(5, 0.3 * 60 * 3, live=False)  # 54 s of drone: the air
    d.add(6, 200)
    d.montage(20, 40)  # the convoy: 40 short clips, half of them drone
    d.add(60, 600)  # the bride's exit: one 10 min take
    for k in range(30):  # the love story: many short staged clips
        d.add(120 + k, 6, talk=0)
    return d


def track(tempo: float = 120.0, minutes: float = 4, confidence: float = 0.9, mid: int = 900):
    period = 60 * S / tempo
    beats = tuple(round(250_000 + k * period) for k in range(int(minutes * MIN / period)))
    return MusicTrack(mid, round(minutes * MIN), Beats(tempo, beats, confidence))


def test_sections_in_shooting_order():
    d = _day()
    kinds = [s.kind for s in sections(list(reversed(d.clips)))]  # input order does not matter
    assert kinds == ["doc", "montage", "doc", "love"]
    first = sections(d.clips)[0]
    assert [c.media_id for c in first.clips] == [1, 3] and [c.media_id for c in first.air] == [2]


def test_film_keeps_rites_live_and_puts_air_between_takes():
    d = _day()
    film = build_film(d.clips, [track()])
    first = [s for s in film.shots if s.section == 0]
    assert [s.media_id for s in first] == [1, 2, 3]  # take, drone air, take
    assert (
        first[1].reason.code == "day_air" and 6 * S <= first[1].src_out - first[1].src_in <= 12 * S
    )
    assert all(s.live for s in first)
    assert film.timeline.video[0].audio_mode is AudioMode.ORIGINAL


def test_long_take_is_cut_to_its_densest_five_minutes():
    d = Day()
    c = d.add(0, 600, talk=0)
    sparse = tuple((t * S, (t + 2) * S) for t in range(0, 400, 6))  # short words, no long quiet
    c = DayClip(c.media_id, c.camera, c.time, c.duration, True, (*sparse, (400 * S, 590 * S)))
    film = build_film([c], [])
    (shot,) = film.shots
    assert shot.src_out - shot.src_in == TAKE_MAX_US
    assert shot.src_in <= 400 * S and shot.src_out >= 590 * S  # the toast is inside


def test_quiet_stretch_inside_a_take_is_dropped():
    d = Day()
    c = d.add(0, 100, talk=0)
    c = DayClip(c.media_id, c.camera, c.time, c.duration, True, ((0, 30 * S), (70 * S, 100 * S)))
    pieces = [(s.src_in // S, s.src_out // S) for s in build_film([c], []).shots]
    assert pieces == [(0, 31), (69, 100)]  # 40 s of quiet gone, 1 s kept around the speech


def test_convoy_becomes_a_beat_clip_about_eleven_to_one():
    d = _day()
    film = build_film(d.clips, [track(120)])
    convoy = [s for s in film.shots if s.section == 1]
    raw = 40 * 12 * S
    total = sum(s.src_out - s.src_in for s in convoy)
    assert abs(total - raw / 11) <= 2 * S
    assert all(s.src_out - s.src_in == 2 * S for s in convoy)  # 4 beats at 120 BPM
    cams = [next(c for c in d.clips if c.media_id == s.media_id).aerial for s in convoy]
    assert True in cams and False in cams
    assert all(s.reason.code == "day_montage" and not s.live for s in convoy)


def test_without_confident_music_shots_are_1_9_s():
    d = _day()
    film = build_film(d.clips, [track(120, confidence=0.2)])
    convoy = [s for s in film.shots if s.section == 1]
    assert {s.src_out - s.src_in for s in convoy} == {1_900_000}


def test_love_story_closes_the_film_as_an_sde():
    d = _day()
    film = build_film(d.clips, [track(128, mid=901), track(96, mid=902)])
    love = [s for s in film.shots if s.reason.code == "day_love"]
    assert love and film.shots[-len(love) :] == tuple(love)
    total = sum(s.src_out - s.src_in for s in love)
    assert 85 * S <= total <= 92 * S
    under_love = [m for m in film.timeline.music if m.media_id == "902"]  # the slow track
    assert under_love and sum(m.duration for m in under_love) == total


def test_sde_shape():
    d = _day()
    sde = build_clip(d.clips, [track(98)], "sde")
    lengths = [s.length for s in sde.shots]
    assert 85 * S <= sum(lengths) <= 92 * S
    assert 1.2 * S <= statistics.median(lengths) <= 2.5 * S
    climax = [s for s in sde.shots if s.part == "climax"]
    jumps = climax[:JUMP_MAX]
    assert len({s.media_id for s in jumps}) == 1  # jump cuts of one take ...
    assert sum(s.media_id == jumps[0].media_id for s in climax) == JUMP_MAX  # ... four, not ten
    assert sde.fade and sde.dissolves  # out of the intro, into the outro
    assert all(s.speed == 1.0 for s in sde.shots)  # 0 fps in the test: no frames for slow motion


def test_sde_slows_long_shots_of_50_fps_footage():
    d = _day()
    clips = [replace(c, fps=50.0) for c in d.clips]
    sde = build_clip(clips, [track(98)], "sde")
    slow = [k for k, s in enumerate(sde.shots) if s.speed == SLOW]
    assert slow and all(b - a > 1 for a, b in pairwise(slow))  # never two slow shots in a row
    for k in slow:
        s = sde.shots[k]
        assert s.part != "climax" and s.src_out - s.src_in == round(s.length * SLOW)
    assert 85 * S <= sde.stats.duration_us <= 92 * S
    assert sde.timeline.duration() == sde.stats.duration_us
    starts = set(accumulate(s.length for s in sde.shots))
    assert set(sde.dissolves) <= starts  # dissolves sit on cuts


def test_express_is_three_minutes_of_the_whole_day():
    d = _day()
    ex = build_clip(d.clips, [track(124)], "express")
    lengths = [s.src_out - s.src_in for s in ex.shots]
    assert 175 * S <= sum(lengths) <= 185 * S
    assert 0.9 * S <= statistics.median(lengths) <= 1.5 * S
    used = {s.media_id for s in ex.shots}
    assert {1, 3, 4} & used and any(m > 50 for m in used)  # from the morning to the love story
    assert ex.timeline.music and ex.timeline.music[0].media_id == "900"


def test_bad_frames_are_avoided_in_montage():
    d = Day()
    for k in range(20):
        d.add(k * 0.5, 12, talk=0, bad=[(0, 12 * S)] if k % 2 else [])
    film = build_film(d.clips, [])
    bad_ids = {c.media_id for c in d.clips if c.bad}
    assert film.shots and not ({s.media_id for s in film.shots} & bad_ids)


def test_layout_live_sound_and_music_tracks():
    d = _day()
    film = build_film(d.clips, [track()])
    items = day_layout(film)
    video = [i for i in items if i.track == "V1"]
    live = [i for i in items if i.track == "A"]
    music = [i for i in items if i.track == "M"]
    assert video[-1].end == film.stats.duration_us
    assert {i.media_id for i in live} >= {1, 3}
    for m in music:  # music only lies under music-driven shots
        assert not any(a.start < m.end and m.start < a.end for a in live if a.media_id in (1, 3))
    assert all(s.track is Track.MUSIC for s in film.timeline.music)


def test_empty_day():
    assert build_film([], []).shots == ()
    assert build_clip([], [], "express").shots == ()


def test_music_does_not_start_over_for_a_short_tail():
    from toystudio.engine.edit.day import LOOP_MIN_US, Shot, _music_under

    r = Reason("x", "x", "x")
    short = MusicTrack(9, 90 * S, Beats(98.0, (S,), 0.9))  # 89 s from its first beat
    shots = [Shot(1, 0, 92 * S, False, r, 0)]
    (piece,) = _music_under(shots, {0: short})  # 3 s short: fade out, no second intro
    assert piece.src_out - piece.src_in == 89 * S
    longer = [Shot(1, 0, 89 * S + LOOP_MIN_US + S, False, r, 0)]
    assert len(_music_under(longer, {0: short})) == 2  # a real loop is still a loop
