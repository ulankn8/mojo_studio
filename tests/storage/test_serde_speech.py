"""The speech track of a timeline survives JSON; timelines saved before it still load."""

import json

from toystudio.core.models import Track
from toystudio.core.timeline import Segment, Timeline
from toystudio.core.timeline_ops import set_locked
from toystudio.storage.serde import timeline_from_json, timeline_to_json


def _seg(i: str, track: Track) -> Segment:
    return Segment(i, track, "7", 0, 5_000_000, None, 0, 10_000_000)


def test_speech_round_trip_and_track_access():
    t = Timeline(video=(_seg("v0", Track.VIDEO),), speech=(_seg("a0", Track.SPEECH),))
    back = timeline_from_json(timeline_to_json(t))
    assert back == t
    assert back.track_segments(Track.SPEECH) == t.speech
    assert set_locked(back, "a0", True).speech[0].locked  # editing commands reach the track


def test_timeline_saved_before_the_speech_track_loads_empty():
    obj = json.loads(timeline_to_json(Timeline(video=(_seg("v0", Track.VIDEO),))))
    del obj["speech"]
    assert timeline_from_json(json.dumps(obj)).speech == ()


# --- slow motion


def test_speed_round_trips_and_old_timelines_play_at_real_speed():
    from dataclasses import replace

    slow = replace(_seg("v0", Track.VIDEO), speed=0.5)
    t = Timeline(video=(slow,))
    assert timeline_from_json(timeline_to_json(t)).video[0].speed == 0.5
    assert t.duration() == 10_000_000  # 5 s of source on 10 s of screen
    obj = json.loads(timeline_to_json(t))
    del obj["video"][0]["speed"]
    assert timeline_from_json(json.dumps(obj)).video[0].speed == 1.0


def test_split_of_a_slow_segment_cuts_the_source_at_half_the_offset():
    import pytest

    from toystudio.core.timeline_ops import split

    t = Timeline(video=(Segment("v0", Track.VIDEO, "7", 0, 4_000_000, None, 0, 9_000_000,
                                speed=0.5),))  # fmt: skip
    a, b = split(t, "v0", 6_000_000, "v1").video  # 6 s on screen = 3 s of source
    assert (a.src_out, b.src_in) == (3_000_000, 3_000_000)
    assert a.duration == 6_000_000 and b.duration == 2_000_000
    with pytest.raises(ValueError):
        Segment("x", Track.VIDEO, "7", 0, 1_000_000, None, 0, 2_000_000, speed=0.0)
