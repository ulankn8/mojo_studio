"""The love story / SDE from the AI director's plan: checked, fitted to the beat and the length.

Claude chooses and orders the shots (toystudio.cloud.director); everything here is local and
deterministic: unknown clips are dropped, windows are moved inside their clips and off bad
frames, repeated windows move further into the clip, and the total is fitted to the target.
"""

from __future__ import annotations

from toystudio.core.ai_plan import AiPlan
from toystudio.core.models import Reason
from toystudio.core.timeline import MIN_SEGMENT_US
from toystudio.engine.edit.day import (
    FALLBACK_BEAT_US,
    DayClip,
    DayDraft,
    MusicTrack,
    Shot,
    _good_start,
    _music_under,
    _stats,
    _timeline,
)

MAX_BEATS = 8


def build_from_plan(
    plan: AiPlan, clips: list[DayClip], track: MusicTrack | None, target_us: int
) -> DayDraft:
    by_id = {c.media_id: c for c in clips}
    beat = track.beat_us if track else FALLBACK_BEAT_US
    # a plan written in fractions (0.3) instead of percent (30): every start is <= 1
    scale = 100.0 if plan.shots and max(s.start_pct for s in plan.shots) <= 1.0 else 1.0
    shots: list[Shot] = []
    used: dict[int, list[tuple[int, int]]] = {}
    total = 0
    for s in plan.shots:
        c = by_id.get(s.clip_id)
        if c is None or total >= target_us:
            continue
        length = min(max(1, min(s.beats, MAX_BEATS)) * beat, c.duration, target_us - total)
        if length < MIN_SEGMENT_US:
            continue
        at = round(min(max(s.start_pct * scale, 0.0), 100.0) / 100 * c.duration)
        at = min(at, c.duration - length)
        for a, b in used.get(c.media_id, []):  # the same window twice: move on in the clip
            if at < b and a < at + length:
                at = b if b + length <= c.duration else max(0, a - length)
        start = _good_start(c, at, length)
        if start is None:
            continue
        reason = Reason("ai_love", s.why, s.why)
        shots.append(Shot(c.media_id, start, start + length, False, reason, 0))
        used.setdefault(c.media_id, []).append((start, start + length))
        total += length
    if shots and total < target_us:  # a short plan: let the last shot run to the target
        last = shots[-1]
        c = by_id[last.media_id]
        grow = min(target_us - total, c.duration - last.src_out)
        if grow > 0:
            shots[-1] = Shot(last.media_id, last.src_in, last.src_out + grow, False, last.reason, 0)
    music = _music_under(shots, {0: track})
    source = [by_id[i] for i in {s.media_id for s in shots}]
    return DayDraft(
        _timeline(shots, music, by_id), tuple(shots), ("love",), _stats(shots, source, 1)
    )
