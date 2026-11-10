from toystudio.core.clock import (
    DAY_US,
    camera_spans,
    offset_from_pair,
    reference_camera,
    shot_time,
    suspicious,
)

S = 1_000_000
H = 3600 * S
T0 = shot_time("2026-06-15T05:37:03Z")  # stab at the groom's house (true UTC)


def test_shot_time_formats():
    assert shot_time("2026-06-15T05:37:03.000000Z") == T0
    assert shot_time("2026-06-15T11:37:03+06:00") == T0
    assert shot_time("2026-06-15T05:37:03") == T0  # naive = UTC
    assert shot_time(None) is None and shot_time("") is None and shot_time("вчера") is None


def _clips():
    stab = [("стаб", T0 + i * 60 * S, 30 * S) for i in range(100)]  # 100 min, 50 min of footage
    drone = [("дрон", T0 - 11 * H + i * 90 * S, 15 * S) for i in range(40)]  # 11 h off
    tripod = [("штатив", T0 + 30 * 60 * S, 600 * S)]  # inside the stab's day
    return stab + drone + tripod


def test_reference_is_the_camera_with_most_footage():
    spans = camera_spans(_clips())
    assert reference_camera(spans) == "стаб"
    assert spans["стаб"].footage_us == 100 * 30 * S


def test_wrong_clock_is_suspicious_good_one_is_not():
    clips = _clips()
    spans = camera_spans(clips)
    assert suspicious({c for c, *_ in clips}, spans, "стаб", {}) == ["дрон"]


def test_year_off_and_no_time_are_suspicious():
    clips = [*_clips(), ("гоупро", T0 - 2437 * DAY_US, 60 * S), ("телефон", None, 60 * S)]
    spans = camera_spans(clips)
    assert suspicious({c for c, *_ in clips}, spans, "стаб", {}) == ["гоупро", "дрон", "телефон"]


def test_saved_correction_is_trusted():
    clips = _clips()
    assert suspicious({c for c, *_ in clips}, camera_spans(clips), "стаб", {"дрон": 11 * H}) == []


def test_offset_from_a_pair_and_no_reference():
    assert offset_from_pair(T0 - 11 * H, T0) == 11 * H
    assert reference_camera({}) is None
    assert suspicious(["дрон"], {}, None, {}) == ["дрон"]
