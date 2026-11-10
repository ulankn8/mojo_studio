"""Restaurant draft from a project: Cut + timeline saved, XML for Premiere written."""

import xml.etree.ElementTree as ET
from datetime import date

import numpy as np
import pytest

from tests.media_fixtures import needs_ffmpeg, write_wav16
from toystudio.core.models import (
    AudioEvent,
    AudioLabel,
    MediaInfo,
    MediaRole,
    ProjectFormat,
    SpeechRegion,
    ToiKind,
)
from toystudio.jobs.restaurant_draft import DraftError, build_draft
from toystudio.storage.project_store import ProjectStore

S = 1_000_000


def _info(duration_s: int, *, video: bool = True) -> MediaInfo:
    return MediaInfo(duration_s * S, 1920 if video else 0, 1080 if video else 0, 0,
                     50.0 if video else 0.0, "h264" if video else "pcm", False, True, video,
                     8_000_000, None)  # fmt: skip


def _project(tmp_path, *, synced: bool = True, fmt=ProjectFormat.WEDDING_FULL):
    root = tmp_path / "Свадьба Муслим"
    s = ProjectStore.create(root, "Свадьба Муслим", ToiKind.WEDDING, date(2026, 6, 15), fmt)
    desk = write_wav16(tmp_path / "src" / "071424-011212.WAV", np.zeros(16_000, np.int16))
    files = [
        (tmp_path / "src" / "C0050.MP4", MediaRole.MAIN, _info(300), 0),
        (tmp_path / "src" / "C0244.MP4", MediaRole.CUTAWAY, _info(300), 0),
        (desk, MediaRole.SPEECH, _info(400, video=False), 0),
        (tmp_path / "src" / "песня.mp3", MediaRole.MUSIC, _info(200, video=False), None),
    ]
    for path, role, info, offset in files:
        mid, _ = s.add_media(path, role, 1, False)
        s.set_media_info(mid, info)
        if role is MediaRole.SPEECH:
            toast = [SpeechRegion(t * S, (t + 3) * S, 0.9) for t in range(20, 140, 4)]
            dance = [AudioEvent(mid, 160 * S, 400 * S, AudioLabel.MUSIC, 0.8)]
            s.replace_audio(mid, toast, dance)
        s.set_sync(mid, offset if synced or role is MediaRole.SPEECH else None, 10.0)
    s.close()
    return root


@needs_ffmpeg
def test_draft_saved_and_xml_written(tmp_path):
    root = _project(tmp_path)
    r = build_draft(root)
    assert r.xml_path == root / "export" / "Свадьба Муслим · ресторан.xml"
    seq = ET.fromstring(r.xml_path.read_text("utf-8").split("\n", 2)[2]).find("sequence")
    v1, v2 = seq.findall("media/video/track")
    assert v1.findall("clipitem") and v2.findall("clipitem")
    assert len(seq.findall("media/audio/track")) == 1  # the mono recorder file
    names = {f.findtext("name") for f in seq.iter("file") if f.findtext("name")}
    assert names == {"C0050.MP4", "C0244.MP4", "071424-011212.WAV"}  # no music, no extras
    assert r.stats.blocks == 2  # the toast and the dance that follows it
    s = ProjectStore.open(root)
    try:
        t = s.load_timeline(r.cut_id)
        assert t is not None and t.video and t.speech
        assert t.duration() == r.stats.duration_us == sum(x.duration for x in t.speech)
    finally:
        s.close()


@needs_ffmpeg
def test_podcast_file_name(tmp_path):
    r = build_draft(_project(tmp_path, fmt=ProjectFormat.PODCAST))
    assert r.xml_path.name == "Свадьба Муслим · черновик.xml"


def test_no_synced_cameras(tmp_path):
    with pytest.raises(DraftError) as e:
        build_draft(_project(tmp_path, synced=False))
    assert e.value.code == "no_synced_cameras"
