"""A finished clip from the layout: length, frame rate, sound, slow motion, dissolves, colour."""

from dataclasses import replace

from tests.media_fixtures import make_clip, needs_ffmpeg
from toystudio.engine.color.grade import ClipGrade, write_cube
from toystudio.engine.edit.restaurant import Item
from toystudio.engine.media.probe import probe_media
from toystudio.engine.render.film import RenderSettings, render_film

S = 1_000_000
SMALL = RenderSettings(320, 180, 25, 30, "ultrafast")


def _sources(tmp_path):
    return {
        1: make_clip(tmp_path / "a.mp4", duration_s=6, fps=50, audio=False),
        2: make_clip(tmp_path / "b.mp4", duration_s=6, fps=50, audio=False),
        3: make_clip(tmp_path / "c.mp4", duration_s=6, fps=25, audio=False),
        9: make_clip(tmp_path / "music.mp4", duration_s=10, tone_hz=220),
    }


@needs_ffmpeg
def test_clip_with_slow_motion_dissolve_colour_and_music(tmp_path):
    src = _sources(tmp_path)
    items = [
        Item("V1", 1, 1 * S, 2 * S, 0, 2 * S, 0.5),  # 1 s of source fills 2 s
        Item("V1", 2, 2 * S, 4 * S, 2 * S, 4 * S),
        Item("V1", 3, 1 * S, 3 * S, 4 * S, 6 * S),
        Item("M", 9, 0, 6 * S, 0, 6 * S),
    ]
    durations = {k: 6 * S for k in (1, 2, 3)} | {9: 10 * S}
    lut = write_cube(tmp_path / "1.cube", ClipGrade(0.02, 1.1, (1.03, 1.0, 0.96)))
    out = render_film(items, src, durations, tmp_path / "out" / "SDE.mp4", luts={1: lut},
                      transitions={4 * S: "fade"}, settings=SMALL)  # fmt: skip
    info = probe_media(out)
    assert abs(info.duration_us - 6 * S) <= 0.1 * S
    assert info.width == 320 and info.height == 180 and round(info.fps) == 25
    assert info.has_audio
    assert not (tmp_path / "out" / "SDE.parts").exists()  # pieces cleaned up


@needs_ffmpeg
def test_no_music_and_progress(tmp_path):
    src = _sources(tmp_path)
    items = [Item("V1", 1, 0, 2 * S, 0, 2 * S), Item("V1", 2, 0, 2 * S, 2 * S, 4 * S)]
    seen: list[float] = []
    out = render_film(items, src, {1: 6 * S, 2: 6 * S}, tmp_path / "x.mp4", settings=SMALL,
                      fade=False, on_progress=seen.append)  # fmt: skip
    info = probe_media(out)
    assert not info.has_audio and abs(info.duration_us - 4 * S) <= 0.1 * S
    assert seen[-1] == 1.0 and seen == sorted(seen)


def test_nothing_to_render(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        render_film([], {}, {}, tmp_path / "x.mp4")


def test_final_quality_settings_and_proxy_sized_preview():
    from toystudio.engine.render.film import _shot_filter

    it = Item("V1", 1, 0, 2 * S, 0, 2 * S)
    final = _shot_filter(it, None, RenderSettings(), 50)
    assert "flags=lanczos" in final and "cas=strength=0.35" in final
    assert RenderSettings().crf <= 16 and RenderSettings().preset == "slow"
    preview = RenderSettings.preview()
    assert (preview.width, preview.height) == (960, 540)  # the proxies' size: no stretching
    assert "cas=" not in _shot_filter(it, None, preview, 50)
    assert RenderSettings.uhd().width == 3840


@needs_ffmpeg
def test_light_overlay_brightens_its_moment_and_keeps_the_length(tmp_path):
    import subprocess

    dark = tmp_path / "dark.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x202020:s=320x180:r=50:d=6", str(dark)], check=True)  # fmt: skip
    light = tmp_path / "light.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0xffc46b:s=320x180:r=25:d=2", str(light)], check=True)  # fmt: skip
    items = [Item("V1", 1, 0, 4 * S, 0, 4 * S),
             Item("V2", -1, 0, 2 * S, 2 * S, 4 * S, 1.0, "screen", 0.8)]  # fmt: skip
    out = render_film(items, {1: dark, -1: light}, {1: 6 * S}, tmp_path / "lit.mp4",
                      fade=False, settings=SMALL)  # fmt: skip
    assert abs(probe_media(out).duration_us - 4 * S) <= 0.1 * S

    def luma(at):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(out),
                              "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "-"],
                             capture_output=True, check=True).stdout  # fmt: skip
        return sum(raw) / len(raw)

    assert luma(3.0) > luma(1.0) + 60  # screen-mode light lifts the dark picture


@needs_ffmpeg
def test_moves_whip_and_flash_to_white_keep_the_length(tmp_path):
    from toystudio.engine.render.film import _move_filter

    src = _sources(tmp_path)
    items = [
        Item("V1", 1, 1 * S, 2 * S, 0, 1 * S),  # a teaser flash
        Item("V1", 2, 2 * S, 4 * S, 1 * S, 3 * S, move="in"),
        Item("V1", 2, 4.5 * S, 5.5 * S, 3 * S, 4 * S, move="turn"),  # a quick pair
    ]
    out = render_film([replace(i, src_in=int(i.src_in), src_out=int(i.src_out)) for i in items],
                      src, {1: 6 * S, 2: 6 * S}, tmp_path / "x.mp4", settings=SMALL,
                      transitions={1 * S: "white", 3 * S: "whip"})  # fmt: skip
    assert abs(probe_media(out).duration_us - 4 * S) <= 0.1 * S
    zoom = _move_filter("in", 320, 180, 2.0)
    assert (
        zoom[0].startswith("scale=w='trunc(320*(1+0.08*(t/2.0000))") and zoom[1] == "crop=320:180"
    )
    assert any(f.startswith("rotate=") for f in _move_filter("turn", 320, 180, 2.0))


def test_storage_keeps_moves_and_reads_old_timelines():
    import json

    from toystudio.core.models import Track
    from toystudio.core.timeline import Segment, Timeline
    from toystudio.storage.serde import timeline_from_json, timeline_to_json

    seg = Segment("v0", Track.VIDEO, "7", 0, S, None, 0, 2 * S, move="out")
    t = Timeline(video=(seg,))
    assert timeline_from_json(timeline_to_json(t)).video[0].move == "out"
    obj = json.loads(timeline_to_json(t))
    del obj["video"][0]["move"]
    assert timeline_from_json(json.dumps(obj)).video[0].move == ""


@needs_ffmpeg
def test_the_mix_is_normalised_for_the_web(tmp_path):
    import re
    import subprocess

    from toystudio.engine.render.film import LOUDNESS_LUFS

    src = _sources(tmp_path)
    items = [Item("V1", 1, 0, 4 * S, 0, 4 * S), Item("M", 9, 0, 4 * S, 0, 4 * S)]
    out = render_film(
        items, src, {1: 6 * S, 9: 10 * S}, tmp_path / "m.mp4", settings=SMALL, fade=False
    )
    log = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(out), "-af", "ebur128=peak=true",
                          "-f", "null", "-"], capture_output=True, text=True, check=True).stderr  # fmt: skip
    lufs = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", log)[-1])
    peak = float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", log)[-1])
    assert abs(lufs - LOUDNESS_LUFS) <= 2 and peak <= -0.5


@needs_ffmpeg
def test_awkward_folder_names(tmp_path):
    """A wedding folder with a comma, an apostrophe, brackets and Cyrillic (and a colon where
    the file system allows it, like a Windows drive): the LUT and the pieces' list still work."""
    import sys

    name = "Свадьба, O'Brien [1]" + ("" if sys.platform == "win32" else " C:x")
    folder = tmp_path / name
    folder.mkdir()
    src = {1: make_clip(folder / "a b.mp4", duration_s=4, fps=25, audio=False)}
    lut = write_cube(folder / "look.cube")
    items = [Item("V1", 1, 0, S, 0, S), Item("V1", 1, 2 * S, 3 * S, S, 2 * S)]
    out = render_film(items, src, {1: 4 * S}, folder / "клип.mp4", luts={1: lut},
                      transitions={S: "fade"}, settings=SMALL)  # fmt: skip
    assert abs(probe_media(out).duration_us - 2 * S) <= 0.1 * S


@needs_ffmpeg
def test_a_locked_old_video_gets_a_new_name(tmp_path, monkeypatch):
    """Windows will not replace a file open in a player: the new video goes beside it."""
    import os

    from toystudio.engine.render import film

    src = _sources(tmp_path)
    old = tmp_path / "SDE.mp4"
    old.write_bytes(b"open in a player")
    real = os.replace

    def locked(a, b):
        if os.fspath(b) == os.fspath(old):
            raise PermissionError(13, "in use")
        real(a, b)

    monkeypatch.setattr(film.os, "replace", locked)
    out = render_film([Item("V1", 1, 0, S, 0, S)], src, {1: 6 * S}, old, settings=SMALL)
    assert out == tmp_path / "SDE (2).mp4" and probe_media(out).duration_us > 0
    assert old.read_bytes() == b"open in a player"
