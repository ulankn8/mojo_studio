"""Restaurant / podcast auto-edit: two cameras switch over one continuous reference sound.

Rules come from the analysis of a professionally edited wedding (toy-studio-materialy/REPORT.md,
rules 15-20): the sound desk recording runs without cuts and the picture switches over it; the
main camera and the cutaway camera alternate (series of ~5.4 s and ~4.5 s); a toast is never
cut inside, whole blocks of pauses and long loud music are dropped; a dance is one long take of
the cutaway camera. All times here are on the reference timeline R (the timeline the sync stage
put every file on), in microseconds.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from toystudio.core.models import MediaRole, Reason, Track
from toystudio.core.timeline import MIN_SEGMENT_US, Segment, Timeline

S = 1_000_000
BIN_US = S  # signals are classified per second
SPEECH_SHARE = 0.3  # a second with >= 30 % speech is talk
MUSIC_SHARE = 0.5  # a second with >= 50 % music (and not talk) is music
PAUSE_KEEP_US = 10 * S  # quiet or short music up to this long stays inside a talk block
DANCE_MIN_US = 30 * S  # music at least this long is a dance block
DANCE_KEEP_US = 300 * S  # a dance keeps at most 5 min, the rest is dropped
PAD_US = S  # talk blocks get 1 s before and after
MIN_BLOCK_US = 3 * S  # shorter talk is a stray word in a pause, not a toast
SHOT_US = {MediaRole.MAIN: 5_400_000, MediaRole.CUTAWAY: 4_500_000}
VARIATION = (1.0, 0.8, 1.25, 0.9, 1.1, 0.95, 1.2, 0.85)  # deterministic, no metronome feel
SNAP_US = S  # a cut moves to a phrase boundary within 1 s
MIN_SHOT_US = 1_500_000
BAD_SHARE_MAX = 0.3  # a camera with more bad frames in the slot gives the slot away

Interval = tuple[int, int]
BlockKind = Literal["talk", "dance"]

_REASONS: dict[BlockKind, Reason] = {
    "talk": Reason(
        "restaurant_talk", "Речь целиком, камеры чередуются", "Сөз толук, камералар алмашат"
    ),
    "dance": Reason("restaurant_dance", "Танец одним длинным планом", "Бий бир узун план менен"),
}


@dataclass(frozen=True)
class Clip:
    """A synced camera file: V1 (main) or V2 (cutaway); `bad` is in the clip's own time."""

    media_id: int
    role: MediaRole
    offset_us: int
    duration_us: int
    bad: tuple[Interval, ...] = ()

    @property
    def start(self) -> int:
        return self.offset_us

    @property
    def end(self) -> int:
        return self.offset_us + self.duration_us


@dataclass(frozen=True)
class RefAudio:
    """A piece of the continuous reference sound: a recorder file or a main camera clip."""

    media_id: int
    offset_us: int
    duration_us: int

    @property
    def end(self) -> int:
        return self.offset_us + self.duration_us


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    kind: BlockKind


@dataclass(frozen=True)
class Shot:
    media_id: int
    start: int
    end: int
    kind: BlockKind


@dataclass(frozen=True)
class DraftStats:
    duration_us: int  # length of the draft
    cuts: int  # video cuts inside blocks and between them
    dropped_us: int  # covered time that did not go into the draft
    blocks: int


@dataclass(frozen=True)
class Draft:
    timeline: Timeline
    blocks: tuple[Block, ...]
    shots: tuple[Shot, ...]
    stats: DraftStats


# ---- intervals


def union(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in sorted(i for i in intervals if i[1] > i[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def intersect(xs: list[Interval], ys: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    i = j = 0
    while i < len(xs) and j < len(ys):
        a, b = max(xs[i][0], ys[j][0]), min(xs[i][1], ys[j][1])
        if a < b:
            out.append((a, b))
        if xs[i][1] < ys[j][1]:
            i += 1
        else:
            j += 1
    return out


def _overlap(a: int, b: int, intervals: tuple[Interval, ...] | list[Interval]) -> int:
    return sum(max(0, min(b, e) - max(a, s)) for s, e in intervals)


# ---- blocks


def _shares(intervals: list[Interval], w0: int, n: int) -> list[float]:
    share = [0.0] * n
    for s, e in union(intervals):
        first, last = max(0, (s - w0) // BIN_US), min(n - 1, (e - 1 - w0) // BIN_US)
        for k in range(first, last + 1):
            b0 = w0 + k * BIN_US
            share[k] += (min(e, b0 + BIN_US) - max(s, b0)) / BIN_US
    return share


def find_blocks(
    covered: list[Interval], speech: list[Interval], music: list[Interval]
) -> list[Block]:
    """Talk blocks (toasts with their short pauses) and capped dance blocks inside `covered`."""
    blocks: list[Block] = []
    for c0, c1 in covered:
        n = math.ceil((c1 - c0) / BIN_US)
        sp, mu = _shares(speech, c0, n), _shares(music, c0, n)
        labels = ["talk" if s >= SPEECH_SHARE else "music" if m >= MUSIC_SHARE else "quiet"
                  for s, m in zip(sp, mu, strict=True)]  # fmt: skip
        runs: list[tuple[str, int, int]] = []  # label, first bin, end bin
        for k, label in enumerate(labels):
            if runs and runs[-1][0] == label:
                runs[-1] = (label, runs[-1][1], k + 1)
            else:
                runs.append((label, k, k + 1))

        def at(k: int, c0: int = c0, c1: int = c1) -> int:
            return min(c0 + k * BIN_US, c1)

        talk: list[Interval] = []
        for i, (label, k0, k1) in enumerate(runs):
            if label == "talk":
                if talk and runs[i - 1][0] != "talk" and _bridge(runs, i):
                    talk[-1] = (talk[-1][0], at(k1))  # short pause or music inside a toast
                else:
                    talk.append((at(k0), at(k1)))
            elif label == "music" and (k1 - k0) * BIN_US >= DANCE_MIN_US:
                blocks.append(Block(at(k0), min(at(k1), at(k0) + DANCE_KEEP_US), "dance"))
        toasts = [(a, b) for a, b in talk if b - a >= MIN_BLOCK_US]
        padded = union([(max(c0, a - PAD_US), min(c1, b + PAD_US)) for a, b in toasts])
        blocks += [Block(a, b, "talk") for a, b in padded]
    return _no_overlap(sorted(blocks, key=lambda b: b.start))


def _bridge(runs: list[tuple[str, int, int]], i: int) -> bool:
    """The run before talk run `i` is a short gap between two talk runs."""
    if i < 2 or runs[i - 2][0] != "talk":
        return False
    label, k0, k1 = runs[i - 1]
    length = (k1 - k0) * BIN_US
    return length <= PAUSE_KEEP_US if label == "quiet" else length < DANCE_MIN_US


def _no_overlap(blocks: list[Block]) -> list[Block]:
    """Padding may push a talk block into a dance: the earlier block wins the shared time."""
    out: list[Block] = []
    for b in blocks:
        if out and b.start < out[-1].end:
            if b.end <= out[-1].end:
                continue
            b = Block(out[-1].end, b.end, b.kind)
        out.append(b)
    return out


# ---- shots


def _pick(clips: list[Clip], role: MediaRole, t: int, end: int) -> Clip | None:
    """The camera for [t, end): good frames first, then the wanted role, then full coverage."""
    cands = [c for c in clips if c.start <= t and c.end - t >= MIN_SEGMENT_US]
    if not cands:
        return None

    def key(c: Clip) -> tuple[bool, bool, bool, int]:
        stop = min(end, c.end)
        bad = _overlap(t - c.offset_us, stop - c.offset_us, c.bad) / max(stop - t, 1)
        return (bad < BAD_SHARE_MAX, c.role is role, c.end >= end, c.end - t)

    return max(cands, key=key)


def _snap(cut: int, lo: int, hi: int, bounds: list[int]) -> int:
    """Nearest phrase boundary to `cut` within SNAP_US, kept inside [lo, hi]."""
    i = bisect_left(bounds, cut)
    near = [b for b in bounds[max(0, i - 1) : i + 1] if abs(b - cut) <= SNAP_US and lo <= b <= hi]
    return min(near, key=lambda b: abs(b - cut)) if near else cut


def talk_shots(block: Block, clips: list[Clip], bounds: list[int]) -> list[Shot]:
    shots: list[Shot] = []
    t, turn, k = block.start, MediaRole.MAIN, 0
    while block.end - t >= MIN_SEGMENT_US:
        end = t + round(SHOT_US[turn] * VARIATION[k % len(VARIATION)])
        if block.end - end < MIN_SHOT_US:
            end = block.end
        else:
            end = _snap(end, t + MIN_SHOT_US, block.end - MIN_SHOT_US, bounds)
        cam = _pick(clips, turn, t, end)
        if cam is None:
            break
        end = min(end, cam.end)
        shots.append(Shot(cam.media_id, t, end, "talk"))
        t, k = end, k + 1
        turn = MediaRole.CUTAWAY if cam.role is MediaRole.MAIN else MediaRole.MAIN
    return shots


def dance_shots(block: Block, clips: list[Clip]) -> list[Shot]:
    shots: list[Shot] = []
    t = block.start
    while block.end - t >= MIN_SEGMENT_US:
        cam = _pick(clips, MediaRole.CUTAWAY, t, block.end)
        if cam is None:
            break
        end = min(block.end, cam.end)
        shots.append(Shot(cam.media_id, t, end, "dance"))
        t = end
    return shots


def _merge(shots: list[Shot]) -> list[Shot]:
    out: list[Shot] = []
    for s in shots:
        if out and out[-1].media_id == s.media_id and out[-1].end == s.start:
            out[-1] = Shot(s.media_id, out[-1].start, s.end, out[-1].kind)
        else:
            out.append(s)
    return out


# ---- the draft


def build(
    clips: list[Clip], refs: list[RefAudio], speech: list[Interval], music: list[Interval]
) -> Draft:
    """Blocks to keep, camera shots inside them and the reference sound under them."""
    cams = union([(c.start, c.end) for c in clips])
    sound = union([(r.offset_us, r.end) for r in refs])
    covered = intersect(cams, sound)
    bounds = sorted({x for s, e in speech for x in (s, e)})
    blocks: list[Block] = []
    shots: list[Shot] = []
    for block in find_blocks(covered, speech, music):
        found = (talk_shots(block, clips, bounds) if block.kind == "talk"
                 else dance_shots(block, clips))  # fmt: skip
        if found:  # the block ends where the cameras did
            blocks.append(Block(block.start, found[-1].end, block.kind))
            shots += _merge(found)
    by_id = {c.media_id: c for c in clips}
    video = tuple(
        Segment(
            id=f"v{i}",
            track=Track.VIDEO,
            media_id=str(s.media_id),
            src_in=s.start - by_id[s.media_id].offset_us,
            src_out=s.end - by_id[s.media_id].offset_us,
            moment_id=None,
            bound_in=0,
            bound_out=by_id[s.media_id].duration_us,
            reasons=(_REASONS[s.kind],),
        )
        for i, s in enumerate(shots)
    )
    under: list[Segment] = []
    for b in blocks:  # recorder files laid back to back: a block may span two of them
        for r in refs:
            a, e = max(b.start, r.offset_us), min(b.end, r.end)
            if e - a >= MIN_SEGMENT_US:
                under.append(
                    Segment(
                        id=f"a{len(under)}",
                        track=Track.SPEECH,
                        media_id=str(r.media_id),
                        src_in=a - r.offset_us,
                        src_out=e - r.offset_us,
                        moment_id=None,
                        bound_in=0,
                        bound_out=r.duration_us,
                    )
                )
    duration = sum(s.end - s.start for s in shots)
    stats = DraftStats(
        duration_us=duration,
        cuts=max(0, len(shots) - 1),
        dropped_us=sum(b - a for a, b in covered) - duration,
        blocks=len(blocks),
    )
    timeline = Timeline(video=video, speech=tuple(under))
    return Draft(timeline, tuple(blocks), tuple(shots), stats)


# ---- layered layout for an NLE: V1 main camera under V2 inserts, sound on its own track


@dataclass(frozen=True)
class Item:
    """A clip on a track of an NLE sequence; all times in microseconds."""

    track: Literal["V1", "V2", "A", "M"]  # A: live or reference sound, M: music
    media_id: int
    src_in: int
    src_out: int
    start: int  # position in the sequence
    end: int
    speed: float = 1.0  # 0.5: slow motion, the source window is half the sequence length
    blend: str = ""  # "screen": a light overlay (black disappears, light adds)
    opacity: float = 1.0
    move: str = ""  # "in" / "out" / "turn" (see Segment.move)


CUT_HALF_US = {"fade": 240_000, "white": 240_000, "whip": 120_000}  # half of each transition


def joins(
    clips: Sequence[Item], durations: Mapping[int, int], cuts: Mapping[int, str]
) -> list[int]:
    """For each cut between neighbouring clips of one track: half the transition it gets (0: a
    hard cut). Only the asked `cuts` (position -> "fade" / "white" / "whip"), and only where
    both clips have that much source (at their speed) beyond the cut."""
    out: list[int] = []
    for a, b in pairwise(clips):
        half = CUT_HALF_US.get(cuts.get(b.start, ""), 0) if a.end == b.start else 0
        fits = (
            b.src_in - round(half * b.speed) >= 0
            and a.src_in + round((a.end - a.start + half) * a.speed) <= durations[a.media_id]
        )
        out.append(half if half and fits else 0)
    return out


def layered(draft: Draft, clips: list[Clip]) -> list[Item]:
    """V1 runs on with the main camera under every cutaway it also filmed, so an editor who
    deletes an insert gets the main picture back; V2 holds the inserts; A the reference sound."""
    by_id = {c.media_id: c for c in clips}
    mains = [c for c in clips if c.role is MediaRole.MAIN]
    items: list[Item] = []
    pos = 0
    for s in draft.shots:
        cam, length = by_id[s.media_id], s.end - s.start
        if cam.role is MediaRole.MAIN:
            items.append(Item("V1", cam.media_id, s.start - cam.offset_us, s.end - cam.offset_us,
                              pos, pos + length))  # fmt: skip
        else:
            items.append(Item("V2", cam.media_id, s.start - cam.offset_us, s.end - cam.offset_us,
                              pos, pos + length))  # fmt: skip
            for m in mains:  # the main camera beneath, where it was filming
                a, e = max(s.start, m.start), min(s.end, m.end)
                if e - a >= MIN_SEGMENT_US:
                    items.append(Item("V1", m.media_id, a - m.offset_us, e - m.offset_us,
                                      pos + a - s.start, pos + e - s.start))  # fmt: skip
        pos += length
    pos = 0
    for seg in draft.timeline.speech:
        items.append(Item("A", int(seg.media_id), seg.src_in, seg.src_out, pos, pos + seg.duration))
        pos += seg.duration
    return _join(items)


def _join(items: list[Item]) -> list[Item]:
    """Merge pieces of one file that follow each other on a track (V1 becomes long takes)."""
    out: list[Item] = []
    for it in sorted(items, key=lambda i: (i.track, i.start)):
        last = out[-1] if out else None
        if (last is not None and last.track == it.track and last.media_id == it.media_id
                and last.end == it.start and last.src_out == it.src_in):  # fmt: skip
            out[-1] = Item(it.track, it.media_id, last.src_in, it.src_out, last.start, it.end)
        else:
            out.append(it)
    return out
