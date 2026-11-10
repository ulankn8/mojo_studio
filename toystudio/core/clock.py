"""Camera clocks: which camera is the reference, which ones are wrong, and the fix from a pair.

Inside one camera the order of its files is reliable; between cameras it is not when someone
never set the date (a gimbal camera in 2019, a drone 11 hours off). The camera with the most
footage is the reference; another camera is suspicious when its shooting does not overlap the
reference's or its date is more than a day away. One pair of clips the user marks as shot at the
same time gives the correction for that whole camera.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from toystudio.core.models import US_PER_S

DAY_US = 24 * 3600 * US_PER_S


def shot_time(creation_time: str | None) -> int | None:
    """creation_time (ISO 8601, 'Z' or offset; naive = UTC) as unix microseconds."""
    if not creation_time:
        return None
    try:
        t = datetime.fromisoformat(creation_time)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return round(t.timestamp() * US_PER_S)


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    footage_us: int  # total length of the camera's clips


def camera_spans(clips: Iterable[tuple[str, int | None, int]]) -> dict[str, Span]:
    """(camera, shot time or None, duration) -> shooting span per camera (timed clips only)."""
    spans: dict[str, Span] = {}
    for camera, t, duration in clips:
        if t is None:
            continue
        old = spans.get(camera)
        if old is None:
            spans[camera] = Span(t, t + duration, duration)
        else:
            spans[camera] = Span(min(old.start, t), max(old.end, t + duration),
                                 old.footage_us + duration)  # fmt: skip
    return spans


def reference_camera(spans: Mapping[str, Span]) -> str | None:
    return max(spans, key=lambda c: (spans[c].footage_us, c)) if spans else None


def suspicious(
    cameras: Iterable[str], spans: Mapping[str, Span], ref: str | None, fixed: Mapping[str, int]
) -> list[str]:
    """Cameras whose clock needs a pair of clips: no time at all, or no overlap with the
    reference / a different day. A camera with a saved correction is trusted."""
    out: list[str] = []
    for cam in sorted(set(cameras)):
        if cam == ref or cam in fixed:
            continue
        span = spans.get(cam)
        if span is None or ref is None:
            out.append(cam)
            continue
        r = spans[ref]
        apart = abs(span.start - r.start) > DAY_US
        if apart or span.end <= r.start or span.start >= r.end:
            out.append(cam)
    return out


def offset_from_pair(cam_clip_time: int, ref_clip_time: int) -> int:
    """Correction that puts the camera's clip at the reference clip's time."""
    return ref_clip_time - cam_clip_time
