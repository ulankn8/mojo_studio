"""FCP7 XML for Premiere: tracks, frames, file references."""

import xml.etree.ElementTree as ET

from toystudio.engine.edit.restaurant import Item
from toystudio.engine.export.premiere_xml import (
    FileRef,
    path_url,
    sequence_xml,
    timebase,
    write_sequence,
)

S = 1_000_000
FILES = {
    1: FileRef(r"C:\Свадьба\штатив\C0050.MP4", 1200 * S, 50.0, 1920, 1080, True, 2),
    2: FileRef("/Volumes/UlanK/стаб/C0244.MP4", 1230 * S, 50.0, 1920, 1080, True, 2),
    10: FileRef(r"D:\Звук\071424-011212.WAV", 5592 * S, 0.0, 0, 0, False, 2),
}
ITEMS = [
    Item("V1", 1, 10 * S, 30 * S, 0, 20 * S),  # main camera, one long take
    Item("V2", 2, 100 * S, 104 * S, 5 * S, 9 * S),  # an insert over it
    Item("A", 10, 40 * S, 60 * S, 0, 20 * S),
]


def _parse(text: str) -> ET.Element:
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>')
    return ET.fromstring(text.split("\n", 2)[2])


def test_tracks_frames_and_files():
    root = _parse(sequence_xml("Ресторан", ITEMS, FILES, 50.0, 1920, 1080))
    seq = root.find("sequence")
    assert seq.findtext("name") == "Ресторан"
    assert seq.findtext("duration") == "1000"  # 20 s at 50 fps
    assert seq.findtext("rate/timebase") == "50" and seq.findtext("rate/ntsc") == "FALSE"
    v1, v2 = seq.findall("media/video/track")
    (main,) = v1.findall("clipitem")
    assert [main.findtext(k) for k in ("start", "end", "in", "out")] == ["0", "1000", "500", "1500"]
    (insert,) = v2.findall("clipitem")
    assert [insert.findtext(k) for k in ("start", "end", "in", "out")] == [
        "250",
        "450",
        "5000",
        "5200",
    ]
    a1, a2 = seq.findall("media/audio/track")  # stereo recorder: left and right
    assert a1.find("clipitem/sourcetrack/trackindex").text == "1"
    assert a2.find("clipitem/sourcetrack/trackindex").text == "2"
    urls = [f.findtext("pathurl") for f in root.iter("file") if f.findtext("pathurl")]
    assert len(urls) == 3  # every file described once, referenced afterwards
    assert "file://localhost/C%3A/%D0%A1%D0%B2%D0%B0%D0%B4%D1%8C%D0%B1%D0%B0/" in urls[0]
    assert urls[1].startswith("file://localhost/Volumes/UlanK/")
    wav = next(f for f in root.iter("file") if f.findtext("name") == "071424-011212.WAV")
    assert wav.find("media/video") is None and wav.findtext("media/audio/channelcount") == "2"


def test_mono_sound_has_one_audio_track():
    files = {**FILES, 10: FileRef("/rec/mono.wav", 600 * S, 0.0, 0, 0, False, 1)}
    seq = _parse(sequence_xml("x", ITEMS, files, 50.0, 1920, 1080)).find("sequence")
    assert len(seq.findall("media/audio/track")) == 1


def test_ntsc_rates():
    assert timebase(29.97) == (30, True)
    assert timebase(25.0) == (25, False)
    seq = _parse(sequence_xml("x", ITEMS, FILES, 30000 / 1001, 1920, 1080)).find("sequence")
    assert seq.findtext("rate/ntsc") == "TRUE"
    assert seq.findtext("duration") == "599"  # 20 s at 29.97


def test_path_urls():
    assert path_url(r"C:\a b\c.mp4") == "file://localhost/C%3A/a%20b/c.mp4"
    assert path_url("/Users/x/a b.mov") == "file://localhost/Users/x/a%20b.mov"


def test_empty_sequence_is_still_valid():
    seq = _parse(sequence_xml("x", [], FILES, 50.0, 1920, 1080)).find("sequence")
    assert seq.findtext("duration") == "0"


def test_write_is_atomic(tmp_path):
    out = tmp_path / "export" / "Свадьба · ресторан.xml"
    write_sequence(out, "x", ITEMS, FILES, 50.0, 1920, 1080)
    assert out.is_file() and not out.with_name(out.name + ".tmp").exists()
    _parse(out.read_text("utf-8"))


# --- slow motion and dissolves


CLIP = {
    3: FileRef("/v/C0157.MP4", 60 * S, 50.0, 1920, 1080, True, 2),
    4: FileRef("/v/C0158.MP4", 60 * S, 50.0, 1920, 1080, True, 2),
}


def _v1(items, **kw):
    seq = _parse(sequence_xml("SDE", items, CLIP, 25.0, 1920, 1080, **kw)).find("sequence")
    return seq.find("media/video/track")


def test_slow_motion_is_a_time_remap_with_half_the_source():
    track = _v1(
        [Item("V1", 3, 10 * S, 12 * S, 0, 4 * S, 0.5), Item("V1", 4, 0, 2 * S, 4 * S, 6 * S)]
    )
    slow, normal = track.findall("clipitem")
    assert [slow.findtext(k) for k in ("start", "end", "in", "out")] == ["0", "100", "250", "300"]
    effect = slow.find("filter/effect")
    assert effect.findtext("effectid") == "timeremap"
    speed = next(p for p in effect.findall("parameter") if p.findtext("parameterid") == "speed")
    assert speed.findtext("value") == "50"
    assert normal.find("filter") is None  # real speed: no filter at all


def test_dissolve_reaches_into_both_handles_and_fades_from_and_to_black():
    items = [Item("V1", 3, 10 * S, 14 * S, 0, 4 * S), Item("V1", 4, 20 * S, 24 * S, 4 * S, 8 * S)]
    track = _v1(items, dissolves=[4 * S], fade=True)
    kinds = [el.tag for el in track]
    assert kinds == ["transitionitem", "clipitem", "transitionitem", "clipitem", "transitionitem"]
    fade_in, mid, fade_out = track.findall("transitionitem")
    assert [mid.findtext(k) for k in ("start", "end", "alignment")] == ["94", "106", "center"]
    assert mid.findtext("effect/effectid") == "Cross Dissolve"
    assert fade_in.findtext("alignment") == "start-black" and fade_in.findtext("start") == "0"
    assert fade_out.findtext("alignment") == "end-black" and fade_out.findtext("end") == "200"
    a, b = track.findall("clipitem")
    assert [a.findtext(k) for k in ("start", "end", "in", "out")] == ["-1", "-1", "250", "356"]
    assert [b.findtext(k) for k in ("start", "end", "in", "out")] == ["-1", "-1", "494", "600"]


def test_dissolve_without_handles_stays_a_cut():
    items = [Item("V1", 3, 56 * S, 60 * S, 0, 4 * S), Item("V1", 4, 0, 4 * S, 4 * S, 8 * S)]
    track = _v1(items, dissolves=[4 * S])
    assert track.find("transitionitem") is None
    assert [c.findtext("start") for c in track.findall("clipitem")] == ["0", "100"]
