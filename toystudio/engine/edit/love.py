"""The love story / SDE from the AI director's plan: checked, fitted to the beat and the length.

Claude chooses and orders the shots (toystudio.cloud.director); everything here is local and
deterministic: unknown clips are dropped, windows are moved inside their clips and off bad
frames, repeated windows move further into the clip, the climax keeps at most JUMP_MAX jump
cuts, a clip is used at most MAX_USES times, look-alike shots are not put side by side, slow
motion is kept only for footage that has the frames for it, and the total is fitted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from toystudio.core.ai_plan import AiPlan, AiReview, AiShot, ReviewOption, ReviewShot
from toystudio.core.models import Reason
from toystudio.core.timeline import MIN_SEGMENT_US
from toystudio.engine.edit.day import (
    FALLBACK_BEAT_US,
    JUMP_MAX,
    SLOW,
    TAIL_US,
    TEASER_FLASHES,
    DayClip,
    DayDraft,
    MusicTrack,
    Shot,
    _good_start,
    _music_under,
    _stats,
    _timeline,
    dissolves,
    finish,
    styles,
)

MAX_BEATS = 8
MAX_USES = 2  # a clip outside the climax
LOOK_ALIKE = 10  # of 64 bits of the average hash: frames this close are the same picture


def _alike(a: int, b: int, looks: Mapping[int, int]) -> bool:
    if a == b:
        return True
    if a not in looks or b not in looks:
        return False
    return (looks[a] ^ looks[b]).bit_count() <= LOOK_ALIKE


def checked(plan: AiPlan, looks: Mapping[int, int] | None = None) -> list[AiShot]:
    """The plan without the repeats a viewer notices: the climax's jump cuts beyond JUMP_MAX,
    a clip's uses beyond MAX_USES, and a shot that looks like the one before it (moved on to
    the next place where it does not, or dropped)."""
    looks = looks or {}
    jumps: dict[int, int] = {}
    uses: dict[int, int] = {}
    kept: list[AiShot] = []
    for s in plan.shots:
        if s.part == "teaser":  # flashes of other moments: not a use of the take
            kept.append(s)
            continue
        if s.part == "climax":
            jumps[s.clip_id] = jumps.get(s.clip_id, 0) + 1
            if jumps[s.clip_id] > JUMP_MAX:
                continue
        else:
            uses[s.clip_id] = uses.get(s.clip_id, 0) + 1
            if uses[s.clip_id] > MAX_USES:
                continue
        kept.append(s)
    out: list[AiShot] = []
    waiting: list[AiShot] = []
    for s in kept:
        for w in list(waiting):  # a postponed shot goes in as soon as it fits
            if out and not _alike(out[-1].clip_id, w.clip_id, looks) and w.part == s.part:
                out.append(w)
                waiting.remove(w)
        if out and s.part != "climax" and _alike(out[-1].clip_id, s.clip_id, looks):
            if s.part != "outro" and len(waiting) < 2:
                waiting.append(s)
            continue
        out.append(s)
    for w in waiting:  # still waiting at the end: in before the outro if it fits there
        at = next((i for i, x in enumerate(out) if x.part == "outro"), len(out))
        if at and not _alike(out[at - 1].clip_id, w.clip_id, looks):
            out.insert(at, w)
    return out


def _beats(s: AiShot) -> int:
    return max(1, min(s.beats, MAX_BEATS))


def build_from_plan(
    plan: AiPlan,
    clips: list[DayClip],
    track: MusicTrack | None,
    target_us: int,
    looks: Mapping[int, int] | None = None,
    *,
    with_teaser: bool = True,
    lively: bool = True,
) -> DayDraft:
    """The plan cut to the beat, checked and fitted to `target_us`; `lively` adds the teaser
    in front and the dynamics (moves, faster aerials, quick pairs) - see day.finish."""
    by_id = {c.media_id: c for c in clips}
    beat = track.beat_us if track else FALLBACK_BEAT_US
    with_teaser = with_teaser and lively and not plan.revised  # a frozen cut has it already
    if with_teaser:  # the teaser comes out of the clip's length, in front of the story
        target_us -= TEASER_FLASHES * beat
    # a plan written in fractions (0.3) instead of percent (30): every start is <= 1
    scale = 100.0 if plan.shots and max(s.start_pct for s in plan.shots) <= 1.0 else 1.0
    shots: list[Shot] = []
    used: dict[int, list[tuple[int, int]]] = {}
    total = 0
    order = list(plan.shots) if plan.revised else checked(plan, looks)  # a frozen cut: checked
    # a plan longer than the clip loses shots from the middle: the climax and the end stay
    ending = sum(
        _beats(s) * beat for s in order if s.part in ("climax", "outro") and s.clip_id in by_id
    )
    for s in order:
        c = by_id.get(s.clip_id)
        if c is None or total >= target_us:
            continue
        speed = SLOW if s.slow and c.can_slow and s.part != "climax" else 1.0
        length = min(_beats(s) * beat, target_us - total)
        if s.part in ("climax", "outro"):
            ending -= _beats(s) * beat
        elif total + length > target_us - ending:
            continue
        src = min(round(length * speed), c.duration)
        length = round(src / speed)
        if src < MIN_SEGMENT_US:
            continue
        at = round(min(max(s.start_pct * scale, 0.0), 100.0) / 100 * c.duration)
        at = min(at, c.duration - src)
        for a, b in used.get(c.media_id, []):  # the same window twice: move on in the clip
            if at < b and a < at + src:
                at = b if b + src <= c.duration else max(0, a - src)
        start = _good_start(c, at, src)
        if start is None:
            continue
        reason = Reason("ai_love", s.why, s.why)
        shots.append(Shot(c.media_id, start, start + src, False, reason, 0, speed, s.part))
        used.setdefault(c.media_id, []).append((start, start + src))
        total += length
    if shots and total < target_us:  # a short plan: let the last shot run to the target
        last = shots[-1]
        c = by_id[last.media_id]
        grow = min(round((target_us - total) * last.speed), c.duration - TAIL_US - last.src_out)
        if grow > 0:
            shots[-1] = Shot(last.media_id, last.src_in, last.src_out + grow, False, last.reason,
                             0, last.speed, last.part)  # fmt: skip
    if shots and lively:
        shots = finish(shots, by_id, beat, with_teaser=with_teaser, pace=not plan.revised)
    music = _music_under(shots, {0: track})
    source = [by_id[i] for i in {s.media_id for s in shots}]
    return DayDraft(_timeline(shots, music, by_id), tuple(shots), ("love",),
                    _stats(shots, source, 1), dissolves(shots), True, beat,
                    styles(shots, frozen=plan.revised))  # fmt: skip


def revised_plan(plan: AiPlan, shots: list[ReviewShot], review: AiReview) -> AiPlan:
    """The cut as it was built, frozen into a plan (each shot's real window and length), with
    the AI's picks swapped in: B / C take that option's window, "drop" leaves the shot out. A
    pick the shot has no option for, or a C clip another shot already took, keeps the shot."""
    picks = {c.shot: c.pick for c in review.changes}
    taken: set[int] = set()
    out: list[AiShot] = []
    for s in shots:
        pick = picks.get(s.index, "A")
        if pick == "drop":
            continue
        options: dict[str, ReviewOption] = {o.label: o for o in s.options}
        o = options.get(pick, options["A"])
        if o.label == "C" and o.clip_id in taken:
            o = options["A"]
        if o.label == "C":
            taken.add(o.clip_id)
        out.append(AiShot(o.clip_id, o.start_pct, s.beats, s.part, s.why, o.speed != 1.0))
    return replace(plan, shots=tuple(out), revised=True)
