"""Editing commands: pure functions Timeline -> Timeline. Changed segments get manual=True."""

from dataclasses import replace
from typing import Literal

from toystudio.core.models import AudioMode, Track
from toystudio.core.timeline import MIN_SEGMENT_US, Segment, Timeline


def _with_track(t: Timeline, track: Track, segs: tuple[Segment, ...]) -> Timeline:
    if track is Track.VIDEO:
        return replace(t, video=segs)
    if track is Track.MUSIC:
        return replace(t, music=segs)
    if track is Track.SPEECH:
        return replace(t, speech=segs)
    return replace(t, titles=segs)


def _update(t: Timeline, seg_id: str, **changes: object) -> Timeline:
    track, i = t.index_of(seg_id)
    segs = t.track_segments(track)
    new = replace(segs[i], manual=True, **changes)  # type: ignore[arg-type]
    return _with_track(t, track, (*segs[:i], new, *segs[i + 1 :]))


def _fit_title(title: Segment, shift: int, length: int, anchor_id: str) -> Segment | None:
    """Re-fit a title window to an anchor of `length`, after moving it by `shift`.

    Returns None (title dropped) when less than MIN_SEGMENT_US of it remains.
    """
    start = max(0, title.src_in + shift)
    end = min(length, title.src_out + shift)
    if end - start < MIN_SEGMENT_US:
        return None
    return replace(
        title, src_in=start, src_out=end, bound_in=0, bound_out=length, anchor_id=anchor_id
    )


def _refit_titles(t: Timeline, anchor_id: str, shift: int, length: int) -> Timeline:
    fitted = (
        _fit_title(x, shift, length, anchor_id) if x.anchor_id == anchor_id else x for x in t.titles
    )
    return replace(t, titles=tuple(x for x in fitted if x is not None))


def move(t: Timeline, seg_id: str, to_index: int) -> Timeline:
    track, i = t.index_of(seg_id)
    segs = list(t.track_segments(track))
    if not 0 <= to_index < len(segs):
        raise ValueError(f"to_index {to_index} out of range for {len(segs)} segments")
    seg = replace(segs.pop(i), manual=True)
    segs.insert(to_index, seg)
    return _with_track(t, track, tuple(segs))


def trim(t: Timeline, seg_id: str, edge: Literal["in", "out"], new_src: int) -> Timeline:
    s = t.segment(seg_id)
    if edge == "in":
        value = max(s.bound_in, min(new_src, s.src_out - MIN_SEGMENT_US))
        out = _update(t, seg_id, src_in=value)
        return _refit_titles(out, seg_id, s.src_in - value, s.src_out - value)
    value = min(s.bound_out, max(new_src, s.src_in + MIN_SEGMENT_US))
    out = _update(t, seg_id, src_out=value)
    return _refit_titles(out, seg_id, 0, value - s.src_in)


def delete(t: Timeline, seg_id: str) -> Timeline:
    track, i = t.index_of(seg_id)
    segs = t.track_segments(track)
    out = _with_track(t, track, (*segs[:i], *segs[i + 1 :]))
    if track is Track.VIDEO:
        out = replace(out, titles=tuple(x for x in out.titles if x.anchor_id != seg_id))
    return out


def split(t: Timeline, seg_id: str, at_timeline: int, new_id: str) -> Timeline:
    track, i = t.index_of(seg_id)
    segs = t.track_segments(track)
    s = segs[i]
    if any(x.id == new_id for tr in Track for x in t.track_segments(tr)):
        raise ValueError(f"segment id {new_id!r} already exists")
    offset = at_timeline - t.starts(track)[i]
    if offset < MIN_SEGMENT_US or s.duration - offset < MIN_SEGMENT_US:
        raise ValueError("split point is too close to a segment edge")
    cut = s.src_in + offset
    first = replace(s, src_out=cut, manual=True)
    second = replace(s, id=new_id, src_in=cut, manual=True)
    out = _with_track(t, track, (*segs[:i], first, second, *segs[i + 1 :]))
    if track is not Track.VIDEO:
        return out
    titles: list[Segment] = []
    for x in out.titles:
        if x.anchor_id != seg_id:
            titles.append(x)
        elif x.src_in >= offset:
            fitted = _fit_title(x, -offset, s.duration - offset, new_id)
            if fitted is not None:
                titles.append(fitted)
        else:
            fitted = _fit_title(x, 0, offset, seg_id)
            if fitted is not None:
                titles.append(fitted)
    return replace(out, titles=tuple(titles))


def set_locked(t: Timeline, seg_id: str, locked: bool) -> Timeline:
    return _update(t, seg_id, locked=locked)


def set_audio_mode(t: Timeline, seg_id: str, mode: AudioMode) -> Timeline:
    return _update(t, seg_id, audio_mode=mode)


def duration_hint(t: Timeline, target_us: int) -> tuple[int, int] | None:
    """(actual, target) when the video deviates from the target by more than 10%."""
    actual = t.duration()
    if abs(actual - target_us) * 10 > target_us:
        return actual, target_us
    return None
