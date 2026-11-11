"""The wedding day before the restaurant: the film, the same-day edit (SDE) and the express.

Rules come from the analysis of a professionally edited wedding (toy-studio-materialy/
REPORT.md, rules 5-14 and 21-25). The day is cut in sections in shooting order:
- documentary sections (rites: the groom's house, the bride's gate, the fourchette) keep long
  takes with their live sound, with a short aerial shot between takes for air;
- montage sections (the convoy, the road) become a music clip of ~4-beat shots, about 11:1;
- the love story is the last montage section of many short clips; it closes the film as an SDE.
The SDE (88 s) and the express (180 s) follow intro -> development -> climax -> outro with
beat-long shots from the middle of the clips. Times of clips are on one clock (camera clocks
corrected), in microseconds.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

from toystudio.core.models import AudioMode, Reason, Track
from toystudio.core.timeline import MIN_SEGMENT_US, Segment, Timeline
from toystudio.engine.audio.beats import CONFIDENT, Beats
from toystudio.engine.edit.restaurant import DraftStats, Interval, Item

S = 1_000_000
DOC_MIN_US = 20 * S  # a take this long with live activity is a rite, not a passing shot
DOC_ACTIVE_SHARE = 0.3  # share of speech or music in the take
DOC_RUN_MIN_US = 40 * S  # shorter documentary runs between montage are montage material
AIR_RUN_MAX = 2  # up to 2 short clips between rites are air for the rite
TAKE_MAX_US = 300 * S  # rites keep at most 5 min per take (the densest window)
QUIET_CUT_US = 10 * S  # quiet stretches inside a take longer than this are dropped
PAD_US = S  # kept around speech when a quiet stretch is dropped
AIR_US = 8 * S  # an aerial shot between takes (rule 7: 6-12 s)
AIR_WINDOW_US = 20 * 60 * S  # an aerial from outside the section must be this close in time
MONTAGE_RATIO = 11  # rule 8: about 11:1
MONTAGE_MIN_US, MONTAGE_MAX_US = 30 * S, 180 * S
SHOT_US = 1_900_000  # rule 8 / 22 when the music has no confident beat
FALLBACK_BEAT_US = 600_000  # beat grid used when the music is unsure (~100 BPM)
DRONE_ENTRY_US = 3_800_000  # rule 9
STAB_ENTRY = 0.39
LOVE_MIN_CLIPS = 20
LOVE_MAX_MEDIAN_US = 12 * S
HEAD_US = 1_000_000  # a take's first second: the couple settles into the pose, the camera steadies
TAIL_US = 600_000  # its last moments: the pose breaks, the operator stops
BAD_MAX = 0.05  # of a shot: a second of shake in a 2 s shot is what a viewer sees
SLOW = 0.5  # slow motion: 50 fps footage on a 25 fps timeline, every frame shown
SLOW_MIN_FPS = 47.0  # 50 / 59.94 / 100 fps; slower footage stays at real speed
LOOP_MIN_US = 8 * S  # a track starts over only for this much more: a second of its intro jars
JUMP_MAX = 4  # jump cuts from one take in the climax (more reads as a loop)
CLIP_FPS = 25.0  # the timeline of the SDE / express (rule: 25p deliverable)

ClipKind = Literal["sde", "express"]
SectionKind = Literal["doc", "montage", "love"]

_R = {
    "doc": Reason("day_doc", "Обряд: длинный дубль с живым звуком", "Салт: узун план, тирүү үн"),
    "air": Reason("day_air", "Воздух: план с дрона между дублями", "Аба: дрондон план"),
    "montage": Reason("day_montage", "Переезд: клип под музыку", "Жол: музыкага клип"),
    "love": Reason("day_love", "Лав-стори в конце фильма", "Лав-стори фильмдин аягында"),
    "clip": Reason("day_clip", "Клип под музыку", "Музыкага клип"),
    "climax": Reason("day_climax", "Кульминация: быстрые склейки", "Туу чокусу: тез кесүү"),
}


@dataclass(frozen=True)
class DayClip:
    media_id: int
    camera: str
    time: int  # start on the common clock
    duration: int
    has_audio: bool
    speech: tuple[Interval, ...] = ()  # clip time
    music: tuple[Interval, ...] = ()
    bad: tuple[Interval, ...] = ()
    fps: float = 0.0

    @property
    def can_slow(self) -> bool:
        return self.fps >= SLOW_MIN_FPS

    @property
    def aerial(self) -> bool:
        """A silent clip (drone): air between rites, intro and outro of clips."""
        return not self.has_audio

    def active_share(self) -> float:
        busy = _covered(list(self.speech) + list(self.music))
        return busy / self.duration if self.duration else 0.0


@dataclass(frozen=True)
class MusicTrack:
    media_id: int
    duration: int
    beats: Beats
    made_for: str = ""  # "sde" / "express": written by the AI for that clip, and preferred

    @property
    def beat_us(self) -> int:
        """The beat to cut on: the track's own when confident, else a neutral ~100 BPM grid."""
        if self.beats.confidence >= CONFIDENT and self.beats.period_us:
            return self.beats.period_us
        return FALLBACK_BEAT_US

    @property
    def first_beat(self) -> int:
        return self.beats.beats_us[0] if self.beats.beats_us else 0


@dataclass
class Section:
    kind: SectionKind
    clips: list[DayClip]
    air: list[DayClip] = field(default_factory=list)


@dataclass(frozen=True)
class Shot:
    media_id: int
    src_in: int
    src_out: int
    live: bool  # its own sound (rites) or the music
    reason: Reason
    section: int
    speed: float = 1.0  # SLOW: the source window is half the shot's length on screen
    part: str = ""  # intro / development / climax / outro in clips

    @property
    def length(self) -> int:
        """Length on the timeline."""
        return round((self.src_out - self.src_in) / self.speed)


@dataclass(frozen=True)
class DayDraft:
    timeline: Timeline
    shots: tuple[Shot, ...]
    sections: tuple[SectionKind, ...]
    stats: DraftStats
    dissolves: tuple[int, ...] = ()  # timeline positions of cuts that cross-dissolve
    fade: bool = False  # from black at the start, to black at the end
    beat_us: int = 0  # the music's beat in a clip (overlays start on beats)


# ---- helpers


def _covered(intervals: list[Interval]) -> int:
    total, end = 0, None
    for a, b in sorted(intervals):
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


def _bad_share(c: DayClip, a: int, b: int) -> float:
    over = sum(max(0, min(b, e) - max(a, s)) for s, e in c.bad)
    return over / max(b - a, 1)


def _good_start(c: DayClip, at: int, length: int) -> int | None:
    """`at` when its window is clean, else the nearest clean start: just after a bad stretch,
    just before one, or further on in steps - later in the clip first (keeps the story's
    order), then earlier. A take's head (HEAD_US) and tail (TAIL_US) are left out as an editor
    trims them; a take too short for that keeps its window in the middle. None when no window
    of `length` in the clip is clean."""
    hi = c.duration - length
    if hi < 0:
        return None
    lo, top = HEAD_US, hi - TAIL_US
    if lo > top:  # too short to trim both ends: as far from them as it allows
        lo = top = max(0, min(hi, round((c.duration - length) * HEAD_US / (HEAD_US + TAIL_US))))
    return _clean_start(c, min(max(lo, at), top), length, lo, top) or _clean_start(
        c, min(max(0, at), hi), length, 0, hi
    )


def _clean_start(c: DayClip, at: int, length: int, lo: int, hi: int) -> int | None:
    """The clean start in [lo, hi] nearest to `at`, later ones first."""
    step = max(length // 2, 200_000)
    starts = {at, *range(at, hi + 1, step), *range(at, lo - 1, -step)}
    for s, e in c.bad:
        starts |= {min(e, hi), max(lo, s - length)}
    clean = [t for t in starts if lo <= t <= hi and _bad_share(c, t, t + length) <= BAD_MAX]
    return min(clean, key=lambda t: (t < at, abs(t - at))) if clean else None


def _spread(clips: list[DayClip], n: int) -> list[DayClip]:
    """n clips evenly over the list (keeps chronology), repeating long ones when n is large."""
    if not clips or n <= 0:
        return []
    weights = [max(c.duration, 1) for c in clips]
    total = sum(weights)
    picks: list[DayClip] = []
    for k in range(n):
        target = (k + 0.5) * total / n
        acc = 0
        for c, w in zip(clips, weights, strict=True):
            acc += w
            if acc >= target:
                picks.append(c)
                break
    return picks


# ---- sections


def is_rite(c: DayClip) -> bool:
    return c.has_audio and c.duration >= DOC_MIN_US and c.active_share() >= DOC_ACTIVE_SHARE


def sections(clips: list[DayClip]) -> list[Section]:
    ordered = sorted(clips, key=lambda c: (c.time, c.media_id))
    runs: list[Section] = []
    for c in ordered:
        kind: SectionKind = "doc" if is_rite(c) else "montage"
        if runs and runs[-1].kind == kind:
            runs[-1].clips.append(c)
        else:
            runs.append(Section(kind, [c]))
    # a short documentary run between montage runs is montage material
    for i, r in enumerate(runs):
        lonely = 0 < i < len(runs) - 1 and runs[i - 1].kind == runs[i + 1].kind == "montage"
        if r.kind == "doc" and lonely and sum(c.duration for c in r.clips) < DOC_RUN_MIN_US:
            r.kind = "montage"
    merged: list[Section] = []
    for r in runs:
        if merged and merged[-1].kind == r.kind:
            merged[-1].clips += r.clips
        else:
            merged.append(r)
    # up to AIR_RUN_MAX short clips between rites are air for the rite before them
    out: list[Section] = []
    i = 0
    while i < len(merged):
        r = merged[i]
        nxt = merged[i + 1] if i + 1 < len(merged) else None
        after = merged[i + 2] if i + 2 < len(merged) else None
        if (r.kind == "doc" and nxt is not None and nxt.kind == "montage"
                and len(nxt.clips) <= AIR_RUN_MAX and after is not None and after.kind == "doc"):  # fmt: skip
            out.append(Section("doc", r.clips + after.clips, r.air + nxt.clips + after.air))
            i += 3
            continue
        out.append(r)
        i += 1
    for s in out:  # silent clips inside a rite section are its air, not its takes
        if s.kind == "doc":
            s.air += [c for c in s.clips if c.aerial]
            s.clips = [c for c in s.clips if not c.aerial]
    last = next((s for s in reversed(out) if s.kind == "montage"), None)
    if (last is not None and len(last.clips) >= LOVE_MIN_CLIPS
            and statistics.median(c.duration for c in last.clips) <= LOVE_MAX_MEDIAN_US):  # fmt: skip
        last.kind = "love"
    return [s for s in out if s.clips or s.air]


# ---- documentary takes


def _take(c: DayClip) -> list[Interval]:
    """The rite take: bad head/tail trimmed, quiet stretches > 10 s dropped, at most 5 min."""
    a, b = 0, c.duration
    while a < b and _bad_share(c, a, min(a + S, b)) >= 0.5:
        a += S
    while b > a and _bad_share(c, max(b - S, a), b) >= 0.5:
        b -= S
    if b - a < MIN_SEGMENT_US:
        return []
    act: list[Interval] = []
    for x, y in sorted((max(a, x), min(b, y)) for x, y in [*c.speech, *c.music]):
        if y <= x:
            continue
        if act and x <= act[-1][1]:
            act[-1] = (act[-1][0], max(act[-1][1], y))
        else:
            act.append((x, y))
    if not act:
        pieces = [(a, b)]
    else:
        pieces = []
        cur = a if act[0][0] - a <= QUIET_CUT_US else act[0][0] - PAD_US
        prev = act[0][1]
        for x, y in act[1:]:
            if x - prev > QUIET_CUT_US:
                pieces.append((cur, prev + PAD_US))
                cur = x - PAD_US
            prev = max(prev, y)
        pieces.append((cur, b if b - prev <= QUIET_CUT_US else prev + PAD_US))
    pieces = [(max(a, x), min(b, y)) for x, y in pieces if min(b, y) - max(a, x) >= MIN_SEGMENT_US]
    if sum(y - x for x, y in pieces) <= TAKE_MAX_US:
        return pieces
    best_at, best = a, -1  # the densest TAKE_MAX_US window of the clip
    for start in range(a, max(a, b - TAKE_MAX_US) + 1, 5 * S):
        dense = _covered([(max(x, start), min(y, start + TAKE_MAX_US)) for x, y in act
                          if y > start and x < start + TAKE_MAX_US])  # fmt: skip
        if dense > best:
            best_at, best = start, dense
    return [(best_at, min(b, best_at + TAKE_MAX_US))]


def _doc_shots(sec: Section, index: int, pool: list[DayClip], used: set[int]) -> list[Shot]:
    shots: list[Shot] = []
    for k, c in enumerate(sec.clips):
        if k > 0:  # air between takes: this section's aerials first, then a near one
            near = [a for a in sec.air if a.media_id not in used] or sorted(
                (a for a in pool if a.media_id not in used
                 and abs(a.time - c.time) <= AIR_WINDOW_US),
                key=lambda a: abs(a.time - c.time),
            )  # fmt: skip
            for a in near[:1]:
                length = min(AIR_US, a.duration)
                at = _good_start(a, min(DRONE_ENTRY_US, a.duration - length), length)
                if at is not None and length >= MIN_SEGMENT_US:
                    # its own (silent) sound: the music is for montage, not for rites
                    shots.append(Shot(a.media_id, at, at + length, True, _R["air"], index))
                    used.add(a.media_id)
        for x, y in _take(c):
            shots.append(Shot(c.media_id, x, y, True, _R["doc"], index))
        used.add(c.media_id)
    return shots


# ---- music-driven shots


def _entry(c: DayClip, middle: bool) -> int:
    if middle:  # rule 25: the central 25-35 % of a staged clip
        return int(c.duration * 0.3)
    return min(DRONE_ENTRY_US, c.duration // 3) if c.aerial else int(c.duration * STAB_ENTRY)


def _cut_shots(
    clips: list[DayClip], lengths: list[int], *, middle: bool
) -> list[tuple[int, int, int]]:
    """For each wanted length a (clip, in, out): clips spread in order, later shots of the same
    clip further into it, bad frames avoided."""
    picks = _spread(clips, len(lengths))
    seen: dict[int, int] = {}
    out: list[tuple[int, int, int]] = []
    for c, length in zip(picks, lengths, strict=True):
        length = min(length, c.duration)
        if length < MIN_SEGMENT_US:
            continue
        k = seen.get(c.media_id, 0)
        seen[c.media_id] = k + 1
        at = _entry(c, middle) + k * length
        if at + length > c.duration:
            at = max(0, c.duration - length - k * MIN_SEGMENT_US)
        start = _good_start(c, at, length)
        if start is not None:
            out.append((c.media_id, start, start + length))
    return out


def _montage_shots(sec: Section, index: int, track: MusicTrack | None) -> list[Shot]:
    raw = sum(c.duration for c in sec.clips)
    target = min(MONTAGE_MAX_US, max(MONTAGE_MIN_US, raw // MONTAGE_RATIO), raw)
    shot = 4 * track.beat_us if track and track.beats.confidence >= CONFIDENT else SHOT_US
    n = max(1, round(target / shot))
    cuts = _cut_shots(sec.clips, [shot] * n, middle=False)
    return [Shot(m, a, b, False, _R["montage"], index) for m, a, b in cuts]


def clip_lengths(target: int, beat: int, kind: ClipKind) -> list[tuple[int, str]]:
    """Shot lengths in beats for intro / development / climax / outro (rules 21-23)."""
    pattern = (3, 2, 4, 3, 2, 3) if kind == "sde" else (2, 2, 1, 3, 2, 1, 2)
    out: list[tuple[int, str]] = []
    total = 0
    intro, outro = 8 * S, 6 * S
    while total < intro:  # long aerial shots
        out.append((4 * beat, "intro"))
        total += 4 * beat
    climax_at, climax_end = int(target * 0.75), int(target * 0.85)
    k = 0
    while total < target - outro:
        if climax_at <= total < climax_end:
            out.append((beat, "climax"))  # jump cuts of one beat
            total += beat
            continue
        length = pattern[k % len(pattern)] * beat
        k += 1
        out.append((length, "dev"))
        total += length
    out.append((max(target - total, beat), "outro"))
    return out


def _clip_shots(
    clips: list[DayClip], target: int, track: MusicTrack | None, kind: ClipKind, index: int
) -> list[Shot]:
    beat = track.beat_us if track else FALLBACK_BEAT_US
    plan = clip_lengths(target, beat, kind)
    aerials = [c for c in clips if c.aerial] or clips
    ground = [c for c in clips if not c.aerial] or clips
    shots: list[Shot] = []
    intro = [length for length, part in plan if part == "intro"]
    dev = [length for length, part in plan if part == "dev"]
    climax = [length for length, part in plan if part == "climax"]
    outro = [length for length, part in plan if part == "outro"]
    first = sorted(aerials, key=lambda c: c.time)
    by_id = {c.media_id: c for c in clips}
    for m, a, b in _cut_shots(first[: max(1, len(intro))], intro, middle=False):
        shots.append(Shot(m, a, b, False, _R["clip"], index, part="intro"))
    dev_cuts = _cut_shots(sorted(clips, key=lambda c: c.time), dev, middle=True)
    # the climax sits where it belongs in time, cut from one long take (jump cuts)
    first_climax = next((i for i, (_, p) in enumerate(plan) if p == "climax"), len(plan))
    split = sum(1 for _, p in plan[:first_climax] if p == "dev")
    late = sorted(ground, key=lambda c: c.time)[len(ground) // 2 :] or ground
    hero = max(late, key=lambda c: c.duration)
    jump: list[Shot] = []
    at = int(hero.duration * 0.3)
    for length in climax[:JUMP_MAX]:
        if at + length > hero.duration:
            at = 0
        jump.append(Shot(hero.media_id, at, min(at + length, hero.duration), False,
                         _R["climax"], index, part="climax"))  # fmt: skip
        at += length + length // 2
    others = [c for c in late if c.media_id != hero.media_id] or late
    rest = climax[JUMP_MAX:]  # the rest of the climax: quick cuts from other takes, two beats
    pairs = [sum(rest[k : k + 2]) for k in range(0, len(rest), 2)]
    jump += [Shot(m, a, b, False, _R["climax"], index, part="climax")
             for m, a, b in _cut_shots(others, pairs, middle=True)]  # fmt: skip
    for m, a, b in dev_cuts[:split]:
        shots.append(_maybe_slow(Shot(m, a, b, False, _R["clip"], index, part="development"),
                                 by_id, beat, shots))  # fmt: skip
    shots += [s for s in jump if s.src_out - s.src_in >= MIN_SEGMENT_US]
    for m, a, b in dev_cuts[split:]:
        shots.append(_maybe_slow(Shot(m, a, b, False, _R["clip"], index, part="development"),
                                 by_id, beat, shots))  # fmt: skip
    last = sorted(aerials, key=lambda c: c.time)[-1:]
    for m, a, b in _cut_shots(last, outro, middle=False):
        shots.append(_maybe_slow(Shot(m, a, b, False, _R["clip"], index, part="outro"),
                                 by_id, beat, shots))  # fmt: skip
    return shots


def _maybe_slow(shot: Shot, by_id: dict[int, DayClip], beat: int, before: list[Shot]) -> Shot:
    """Long shots (3+ beats) from 50 fps footage play at half speed, never two in a row: the
    same length on screen from half the source window, around the middle of the window."""
    c = by_id[shot.media_id]
    length = shot.src_out - shot.src_in
    if not c.can_slow or length < 3 * beat or (before and before[-1].speed != 1.0):
        return shot
    src = round(length * SLOW)
    at = shot.src_in + (length - src) // 2
    return Shot(shot.media_id, at, at + src, shot.live, shot.reason, shot.section, SLOW, shot.part)


def dissolves(shots: list[Shot] | tuple[Shot, ...]) -> tuple[int, ...]:
    """Cuts that cross-dissolve in a clip: out of the intro, into the outro, and every second
    cut into a slow shot (never into the climax: it hits on a hard cut)."""
    out: list[int] = []
    pos = 0
    into_slow = 0
    for prev, cur in pairwise(shots):
        pos += prev.length
        if cur.part == "climax":
            continue
        if cur.speed != 1.0:
            into_slow += 1
        if (
            (prev.part == "intro" and cur.part != "intro")
            or (cur.part == "outro" and prev.part != "outro")
            or (cur.speed != 1.0 and into_slow % 2 == 1)
        ):
            out.append(pos)
    return tuple(out)


# ---- assembly


def _pick_tracks(tracks: list[MusicTrack]) -> tuple[list[MusicTrack], MusicTrack | None]:
    """Fast tracks for the montage, the slowest for the love story; music the AI wrote for
    the express / the love story comes first for it (the newest when there are several)."""
    by_tempo = sorted(tracks, key=lambda t: (-(t.beats.tempo or 0), t.media_id))
    made_fast = sorted((t for t in tracks if t.made_for == "express"), key=lambda t: -t.media_id)
    fast = made_fast + [t for t in by_tempo if t.made_for != "express"]
    made = [t for t in tracks if t.made_for == "sde"]
    slow = max(made, key=lambda t: t.media_id) if made else (by_tempo[-1] if by_tempo else None)
    return fast, slow


def _music_under(shots: list[Shot], tracks: dict[int, MusicTrack | None]) -> list[Segment]:
    """One music piece per section, from the section's track (looped), starting on a beat."""
    pieces: list[Segment] = []
    by_section: dict[int, int] = {}
    order: list[int] = []
    for s in shots:
        if s.section not in by_section:
            order.append(s.section)
            by_section[s.section] = 0
        by_section[s.section] += s.length
    for sec in order:
        track, need = tracks.get(sec), by_section[sec]
        first = True
        start = track.first_beat if track else 0
        if track is not None and track.made_for and track.duration - start >= need:
            # music written for the clip ends on its own last chord: end the clip with it
            beat = track.beat_us
            start += (track.duration - start - need) // beat * beat
        while need > 0 and track is not None and track.duration > track.first_beat:
            at = start if first else track.first_beat
            take = min(need, track.duration - at)
            if take < (MIN_SEGMENT_US if first else LOOP_MIN_US):
                break
            first = False
            pieces.append(Segment(f"m{len(pieces)}", Track.MUSIC, str(track.media_id), at,
                                  at + take, None, 0, track.duration))  # fmt: skip
            need -= take
    return pieces


def _timeline(shots: list[Shot], music: list[Segment], by_id: dict[int, DayClip]) -> Timeline:
    video = tuple(
        Segment(
            id=f"v{i}",
            track=Track.VIDEO,
            media_id=str(s.media_id),
            src_in=s.src_in,
            src_out=s.src_out,
            moment_id=None,
            bound_in=0,
            bound_out=by_id[s.media_id].duration,
            audio_mode=AudioMode.ORIGINAL if s.live else AudioMode.MUSIC,
            reasons=(s.reason,),
            speed=s.speed,
        )
        for i, s in enumerate(shots)
    )
    return Timeline(video=video, music=tuple(music))


def _stats(shots: list[Shot], clips: list[DayClip], n_sections: int) -> DraftStats:
    duration = sum(s.length for s in shots)
    return DraftStats(duration, max(0, len(shots) - 1),
                      sum(c.duration for c in clips) - duration, n_sections)  # fmt: skip


def build_film(clips: list[DayClip], tracks: list[MusicTrack]) -> DayDraft:
    """The film before the restaurant: rites, montage clips, the love story at the end."""
    secs = sections(clips)
    by_id = {c.media_id: c for c in clips}
    fast, slow = _pick_tracks(tracks)
    pool = [c for c in clips if c.aerial]
    used: set[int] = set()
    shots: list[Shot] = []
    music_of: dict[int, MusicTrack | None] = {}
    montage_no = 0
    love: list[tuple[int, Section]] = []
    for i, sec in enumerate(secs):
        if sec.kind == "love":
            love.append((i, sec))
            continue
        if sec.kind == "doc":
            shots += _doc_shots(sec, i, pool, used)
        else:
            track = fast[montage_no % len(fast)] if fast else None
            montage_no += 1
            music_of[i] = track
            shots += _montage_shots(sec, i, track)
    for i, sec in love:  # rule 14: the love story (the SDE) closes the film
        music_of[i] = slow
        shots += [Shot(s.media_id, s.src_in, s.src_out, False, _R["love"], i)
                  for s in _clip_shots(sec.clips, 88 * S, slow, "sde", i)]  # fmt: skip
    music = _music_under([s for s in shots if not s.live], music_of)
    return DayDraft(_timeline(shots, music, by_id), tuple(shots), tuple(s.kind for s in secs),
                    _stats(shots, clips, len(secs)))  # fmt: skip


SDE_US = 88 * S


def love_source(clips: list[DayClip]) -> list[DayClip]:
    """The love story's clips: its section of the day, or every clip when there is none."""
    love = next((s for s in sections(clips) if s.kind == "love"), None)
    return love.clips if love else sorted(clips, key=lambda c: (c.time, c.media_id))


def slow_track(tracks: list[MusicTrack]) -> MusicTrack | None:
    """The love story's music: the slowest track of the library."""
    return _pick_tracks(tracks)[1]


def build_clip(clips: list[DayClip], tracks: list[MusicTrack], kind: ClipKind) -> DayDraft:
    """SDE (88 s from the love story) or express (180 s from the whole day)."""
    secs = sections(clips)
    by_id = {c.media_id: c for c in clips}
    if kind == "sde":
        source = love_source(clips)
        target = SDE_US
        track = slow_track(tracks)
    else:
        source, target = clips, 180 * S
        fast = _pick_tracks(tracks)[0]
        track = fast[0] if fast else None
    if not source:
        return DayDraft(Timeline(), (), tuple(s.kind for s in secs), DraftStats(0, 0, 0, 0))
    shots = _clip_shots(source, target, track, kind, 0)
    music = _music_under(shots, {0: track})
    beat = track.beat_us if track else FALLBACK_BEAT_US
    return DayDraft(_timeline(shots, music, by_id), tuple(shots), tuple(s.kind for s in secs),
                    _stats(shots, source, 1), dissolves(shots), True, beat)  # fmt: skip


def day_layout(draft: DayDraft) -> list[Item]:
    """V1 the shots; A the live sound of rites; M the music under the music-driven shots."""
    items: list[Item] = []
    pos = 0
    music_spans: list[Interval] = []
    for seg in draft.timeline.video:
        end = pos + seg.duration
        items.append(Item("V1", int(seg.media_id), seg.src_in, seg.src_out, pos, end, seg.speed))
        if seg.audio_mode is AudioMode.ORIGINAL:
            items.append(Item("A", int(seg.media_id), seg.src_in, seg.src_out, pos,
                              pos + seg.duration))  # fmt: skip
        else:
            music_spans.append((pos, pos + seg.duration))
        pos += seg.duration
    pos = 0
    for m in draft.timeline.music:  # music pieces run back to back over the music-driven shots
        a = pos
        for s, e in music_spans:
            if e <= a:
                continue
            a = max(a, s)
            break
        length = m.duration
        items.append(Item("M", int(m.media_id), m.src_in, m.src_out, a, a + length))
        pos = a + length
    return items
