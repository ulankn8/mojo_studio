"""A finished clip from the layout: length, frame rate, sound, slow motion, dissolves, colour."""

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
                      dissolves=[4 * S], settings=SMALL)  # fmt: skip
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
