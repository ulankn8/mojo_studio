"""Day draft from a project: camera clocks first, then film / express / love story XML."""

import json
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.media_fixtures import make_clip, needs_ffmpeg
from toystudio.core.clock import shot_time
from toystudio.core.models import (
    MediaInfo,
    MediaRole,
    ProjectFormat,
    QualityInterval,
    QualityLabel,
    SpeechRegion,
    ToiKind,
)
from toystudio.engine.media.probe import probe_media
from toystudio.jobs.day_draft import build_day_draft, clocks
from toystudio.jobs.restaurant_draft import DraftError
from toystudio.storage.project_store import ProjectStore

S = 1_000_000
DAY = datetime(2026, 6, 15, 5, 30, tzinfo=UTC)


def _info(seconds: float, at: datetime, *, audio: bool = True) -> MediaInfo:
    return MediaInfo(round(seconds * S), 1920, 1080, 0, 50.0, "h264", False, audio, True,
                     8_000_000, at.isoformat())  # fmt: skip


def _project(tmp_path, *, drone_hours_off: float = 11.0):
    root = tmp_path / "Свадьба"
    s = ProjectStore.create(root, "Свадьба", ToiKind.WEDDING, date(2026, 6, 15),
                            ProjectFormat.WEDDING_FULL)  # fmt: skip
    src = tmp_path / "src"

    def add(folder: str, name: str, seconds: float, at: datetime, audio=True, talk=True):
        mid, _ = s.add_media(src / folder / name, MediaRole.PRE, 1, False)
        s.set_media_info(mid, _info(seconds, at, audio=audio))
        if talk:
            s.replace_audio(mid, [SpeechRegion(0, round(seconds * 0.8 * S), 0.9)], [])
        return mid

    add("стаб", "C0001.MP4", 120, DAY)  # groom's house
    add("стаб", "C0002.MP4", 150, DAY + timedelta(minutes=5))
    for k in range(30):  # the convoy
        add("стаб", f"C{k + 3:04d}.MP4", 10, DAY + timedelta(minutes=30 + k), talk=False)
    for k in range(10):
        off = DAY + timedelta(minutes=31 + 2 * k, hours=-drone_hours_off)
        add("дрон", f"DJI_{k:04d}.MP4", 15, off, audio=False, talk=False)
    s.close()
    return root


def test_wrong_drone_clock_asks_for_a_pair_then_drafts(tmp_path):
    root = _project(tmp_path)
    with pytest.raises(DraftError) as e:
        build_day_draft(root, "film")
    assert e.value.code == "clocks"
    s = ProjectStore.open(root)
    try:
        c = clocks(s)
        assert c.reference.endswith("стаб") and [x.rsplit("/", 1)[-1] for x in c.to_fix] == ["дрон"]
        drone, stab = c.clips[c.to_fix[0]][0], c.clips[c.reference][30]
        offset = shot_time(stab.info.creation_time) - shot_time(drone.info.creation_time)
        s.set_camera_clock(c.to_fix[0], offset)
        assert clocks(s).to_fix == []
    finally:
        s.close()
    r = build_day_draft(root, "film")
    assert r.xml_path.name == "Свадьба · до ресторана.xml"
    seq = ET.fromstring(r.xml_path.read_text("utf-8").split("\n", 2)[2]).find("sequence")
    assert seq.findall("media/video/track/clipitem")
    names = [c.findtext("name") for c in seq.findall("media/video/track/clipitem")]
    assert names[0] == "C0001.MP4" and "DJI_0000.MP4" in names  # rites first, drone in the convoy


def test_skipping_the_clock_spreads_the_camera_over_the_day(tmp_path):
    root = _project(tmp_path)
    r = build_day_draft(root, "express", skip_clocks=True)
    assert r.xml_path.name == "Свадьба · экспресс.xml" and r.stats.duration_us > 100 * S


def test_good_clocks_need_no_pair(tmp_path):
    root = _project(tmp_path, drone_hours_off=0)
    s = ProjectStore.open(root)
    try:
        assert clocks(s).to_fix == []
    finally:
        s.close()
    assert build_day_draft(root, "sde").xml_path.name == "Свадьба · love story.xml"


def test_project_without_shooting_before_the_restaurant(tmp_path):
    root = tmp_path / "Пусто"
    ProjectStore.create(root, "Пусто", ToiKind.WEDDING, date(2026, 1, 1)).close()
    with pytest.raises(DraftError) as e:
        build_day_draft(root, "film")
    assert e.value.code == "nothing_to_keep"


# --- AI director inputs and a draft from its plan ---


@needs_ffmpeg
def test_love_inputs_make_small_frames_and_flags(tmp_path):
    from toystudio.engine.analysis.single_pass import outputs_for
    from toystudio.jobs.day_draft import love_inputs

    root = _project(tmp_path, drone_hours_off=0)
    s = ProjectStore.open(root)
    try:
        ids = [m.id for m in s.list_media()]
        for mid in ids:  # tiny proxies stand in for the analysed ones
            make_clip(outputs_for(root / "cache", mid).proxy, duration_s=12, size="640x360")
        shaky = ids[5]
        s.replace_quality(shaky, [QualityInterval(shaky, 0, 9_000_000, QualityLabel.SHAKY)])
    finally:
        s.close()
    inp = love_inputs(root)
    assert inp.clips and inp.target_us == 88 * S and inp.beat_us == 600_000
    first = inp.clips[0]
    assert len(first.frames) == 3 and first.fps == 50.0 and all(f.is_file() for f in first.frames)
    assert probe_media(first.frames[0]).width == 320
    flagged = {c.id: c.flags for c in inp.clips}
    assert flagged.get(shaky) == ("трясётся",)


def test_draft_from_an_ai_plan(tmp_path):
    from toystudio.core.ai_plan import AiPlan, AiShot

    root = _project(tmp_path, drone_hours_off=0)
    s = ProjectStore.open(root)
    try:
        convoy = [m.id for m in s.list_media() if m.path.name.startswith("C00")][2:12]
    finally:
        s.close()
    plan = AiPlan("Кортеж и встреча", tuple(AiShot(i, 30.0, 3, "development", "кортеж")
                                             for i in convoy))  # fmt: skip
    r = build_day_draft(root, "sde", ai_plan=plan)
    assert r.xml_path.name == "Свадьба · love story ИИ.xml"
    # 10 shots x 3 beats x 0.6 s = 18 s; the last one runs on to the end of its 10 s clip
    assert r.stats.duration_us == 18 * S + (10 * S - 4_800_000)
    saved = json.loads((root / "cache" / "ai" / "love_plan.json").read_text("utf-8"))
    assert saved["story"] == "Кортеж и встреча" and len(saved["shots"]) == 10


def test_clip_drafts_are_25p_with_dissolves_and_fades(tmp_path):
    root = _project(tmp_path, drone_hours_off=0)
    r = build_day_draft(root, "express")
    seq = ET.fromstring(r.xml_path.read_text("utf-8").split("\n", 2)[2]).find("sequence")
    assert seq.findtext("rate/timebase") == "25"  # 50 fps footage slows down cleanly on it
    aligns = [t.findtext("alignment") for t in seq.iter("transitionitem")]
    assert aligns[0] == "start-black" and aligns[-1] == "end-black"
    film = build_day_draft(root, "film")
    seq = ET.fromstring(film.xml_path.read_text("utf-8").split("\n", 2)[2]).find("sequence")
    assert seq.findtext("rate/timebase") == "50" and seq.find(".//transitionitem") is None


@needs_ffmpeg
def test_average_hash_tells_scenes_apart(tmp_path):
    import subprocess

    from toystudio.engine.media.thumbs import average_hash

    def still(name, source):
        out = tmp_path / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source,
                        "-frames:v", "1", str(out)], check=True)  # fmt: skip
        return average_hash(out)

    a = still("a.jpg", "testsrc2=size=320x180")
    b = still("b.jpg", "testsrc2=size=320x180,eq=brightness=0.05")
    c = still("c.jpg", "mandelbrot=size=320x180")
    assert (a ^ b).bit_count() <= 10 < (a ^ c).bit_count()


# --- the look LUT, the saved plan, the finished video


def test_clip_draft_puts_the_look_next_to_the_xml(tmp_path):
    root = _project(tmp_path, drone_hours_off=0)
    r = build_day_draft(root, "express")
    cube = r.xml_path.parent / "T-Studio Тёплое кино.cube"
    assert cube.is_file() and "LUT_3D_SIZE 33" in cube.read_text("utf-8")


def test_saved_plan_round_trip(tmp_path):
    from toystudio.core.ai_plan import AiPlan, AiShot
    from toystudio.jobs.day_draft import saved_plan

    root = _project(tmp_path, drone_hours_off=0)
    assert saved_plan(root) is None
    s = ProjectStore.open(root)
    try:
        ids = [m.id for m in s.list_media()][2:6]
    finally:
        s.close()
    plan = AiPlan("Кортеж", tuple(AiShot(i, 30.0, 3, "development", "кортеж", i % 2 == 0)
                                  for i in ids), "claude-opus-5-5", 100, 50, 4)  # fmt: skip
    build_day_draft(root, "sde", ai_plan=plan)
    assert saved_plan(root) == plan


@needs_ffmpeg
def test_render_preview_from_proxies(tmp_path, monkeypatch):
    from toystudio.engine.analysis.single_pass import outputs_for
    from toystudio.engine.render.film import RenderSettings
    from toystudio.jobs.day_draft import render_draft

    small = RenderSettings(320, 180, 25, 35, "ultrafast", vignette=False)
    monkeypatch.setattr(RenderSettings, "preview", staticmethod(lambda: small))

    root = _project(tmp_path, drone_hours_off=0)
    s = ProjectStore.open(root)
    try:
        for m in s.list_media():
            secs = m.info.duration_us / 1_000_000
            make_clip(outputs_for(root / "cache", m.id).proxy, duration_s=secs, fps=50,
                      size="320x180", audio=False)  # fmt: skip
    finally:
        s.close()
    seen: list[float] = []
    r = render_draft(root, "sde", preview=True, on_progress=seen.append)
    assert r.video.name == "Свадьба · love story · превью.mp4" and not r.from_proxy
    info = probe_media(r.video)
    assert info.width == 320 and round(info.fps) == 25 and info.duration_us > 60 * 1_000_000
    assert seen[-1] == 1.0
    assert list((root / "cache" / "grade").glob("*.cube"))  # every shot's clip has its colour


def test_render_without_files_or_plan_explains(tmp_path):
    from toystudio.jobs.day_draft import render_draft

    root = _project(tmp_path, drone_hours_off=0)
    with pytest.raises(DraftError) as e:
        render_draft(root, "sde_ai")
    assert e.value.code == "no_plan"
    with pytest.raises(DraftError) as e:
        render_draft(root, "express")  # neither the originals nor proxies exist
    assert e.value.code == "missing_files"


@needs_ffmpeg
def test_love_story_avoids_hand_shake_measured_on_the_proxy(tmp_path):
    import numpy as np

    from toystudio.engine.analysis.single_pass import outputs_for
    from toystudio.jobs.day_draft import _shaky

    root = _project(tmp_path, drone_hours_off=0)
    s = ProjectStore.open(root)
    try:
        mid = s.list_media()[0].id
        make_clip(outputs_for(root / "cache", mid).proxy, duration_s=3, fps=50, size="320x180")
        first = _shaky(s, {mid})
        cached = root / "cache" / "jitter" / f"{mid}.npz"
        assert cached.is_file() and first[mid] == []  # the test pattern scrolls smoothly
        np.savez(cached, values=np.full(150, 3.0), fps=50.0)  # pretend the hand trembled
        assert _shaky(s, {mid})[mid] == [(0, 3_000_000)]  # read from the cache, not decoded
    finally:
        s.close()
