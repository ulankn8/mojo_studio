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
