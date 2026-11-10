import sqlite3
from dataclasses import replace
from importlib import resources
from pathlib import Path

from tests.storage.test_project_store_analysis import add_video, new_store
from toystudio.core.models import (
    AudioEvent,
    AudioLabel,
    Mark,
    MediaStatus,
    MomentRow,
    MomentType,
    QualityLabel,
    Reason,
    SpeechRegion,
)
from toystudio.storage.project_store import ProjectStore


def moment(media_id, start, end, type_=MomentType.TOAST, **kw) -> MomentRow:
    return MomentRow(
        id=0,
        media_id=media_id,
        type=type_,
        start_us=start,
        end_us=end,
        score=kw.pop("score", 0.8),
        quality_label=kw.pop("quality_label", QualityLabel.CLEAR),
        **kw,
    )


def test_replace_audio_roundtrip_and_idempotent(tmp_path):
    s = new_store(tmp_path)
    mid = add_video(s, tmp_path)
    other = add_video(s, tmp_path, "B.MP4")
    regions = [SpeechRegion(0, 2_000_000, 0.9), SpeechRegion(5_000_000, 6_000_000, 0.7)]
    events = [
        AudioEvent(mid, 1_000_000, 3_000_000, AudioLabel.APPLAUSE, 0.6),
        AudioEvent(mid, 0, 500_000, AudioLabel.MUSIC, 0.4),
    ]
    s.replace_audio(other, [SpeechRegion(1, 2, 0.5)], [])
    s.replace_audio(mid, regions, events)
    s.replace_audio(mid, regions, events)
    assert s.speech_regions(mid) == regions
    assert s.audio_events(mid) == sorted(events, key=lambda e: e.start_us)
    assert s.speech_regions(other) == [SpeechRegion(1, 2, 0.5)]
    s.replace_audio(mid, [], [])
    assert s.speech_regions(mid) == [] and s.audio_events(mid) == []
    assert len(s.speech_regions(other)) == 1


def test_replace_moments_keeps_marks_of_matching_moments(tmp_path):
    s = new_store(tmp_path)
    mid = add_video(s, tmp_path)
    reasons = (Reason("speech", "Речь", "Сөз"),)
    ids = s.replace_moments(
        mid,
        [
            moment(mid, 0, 10_000_000, reasons=reasons, preview="cache/thumbs/a.jpg"),
            moment(mid, 20_000_000, 30_000_000),
            moment(mid, 40_000_000, 50_000_000, MomentType.DANCE),
        ],
    )
    s.set_mark(ids[0], Mark.MUST)
    s.set_mark(ids[1], Mark.EXCLUDE)
    s.set_mark(ids[2], Mark.MUST)
    new_ids = s.replace_moments(
        mid,
        [
            moment(mid, 1_000_000, 11_000_000),  # overlaps first (90 %)
            moment(mid, 26_000_000, 36_000_000),  # 4 of 10 s = 40 % -> no carry
            moment(mid, 40_000_000, 50_000_000, MomentType.TOAST),  # other type -> no carry
            moment(mid, 41_000_000, 49_000_000, MomentType.DANCE),  # contained, 100 % of shorter
        ],
    )
    assert len(new_ids) == 4 and not set(new_ids) & set(ids)
    got = {m.id: m for m in s.moments(mid)}
    assert [got[i].mark for i in new_ids] == [Mark.MUST, Mark.NONE, Mark.NONE, Mark.MUST]
    # one old moment carries over to at most one new moment
    again = s.replace_moments(mid, [moment(mid, 0, 10_000_000), moment(mid, 0, 9_000_000)])
    marks = [m.mark for m in s.moments(mid) if m.id in again]
    assert marks.count(Mark.MUST) == 1
    assert all(m.media_id == mid for m in s.moments())


def test_moment_fields_roundtrip_and_group_and_mark(tmp_path):
    s = new_store(tmp_path)
    mid = add_video(s, tmp_path)
    r = (Reason("applause", "Аплодисменты", "Кол чабуу"), Reason("speech", "Речь", "Сөз"))
    (i,) = s.replace_moments(
        mid,
        [
            moment(
                mid, 5, 9, reasons=r, preview="cache/thumbs/x.jpg", quality_label=QualityLabel.DARK
            )
        ],
    )
    m = s.moments(mid)[0]
    assert m == replace(
        moment(mid, 5, 9, reasons=r, preview="cache/thumbs/x.jpg", quality_label=QualityLabel.DARK),
        id=i,
    )
    s.set_group_ids([(i, 7)])
    assert s.moments(mid)[0].group_id == 7
    s.set_group_ids([(i, None)])
    assert s.moments(mid)[0].group_id is None
    s.set_previews([(i, f"cache/thumbs/m{i}.jpg")])
    assert s.moments(mid)[0].preview == f"cache/thumbs/m{i}.jpg"
    s.set_previews([(i, None)])
    assert s.moments(mid)[0].preview is None


def test_moment_counts(tmp_path):
    s = new_store(tmp_path)
    a = add_video(s, tmp_path)
    b = add_video(s, tmp_path, "B.MP4")
    bad = add_video(s, tmp_path, "C.MP4")
    s.replace_moments(a, [moment(a, 0, 5), moment(a, 6, 9), moment(a, 10, 12, MomentType.DANCE)])
    s.replace_moments(b, [moment(b, 0, 5)])
    s.replace_moments(bad, [moment(bad, 0, 5), moment(bad, 0, 5, MomentType.GIFTS)])
    s.set_media_status(bad, MediaStatus.CORRUPT)
    assert s.moment_counts() == {MomentType.TOAST: 3, MomentType.DANCE: 1}


def test_migration_from_v2_keeps_data(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    con = sqlite3.connect(root / "project.db")
    for name in ("0001_init.sql", "0002_analysis.sql"):
        con.executescript(
            resources.files("toystudio.storage")
            .joinpath("migrations", name)
            .read_text(encoding="utf-8")
        )
    con.execute("INSERT INTO project VALUES (1, 'Той', 'wedding', NULL, 'n', 'n', 2)")
    con.execute("INSERT INTO media(id, kind, path, size) VALUES (1, 'video', 'a.mp4', 5)")
    con.execute(
        "INSERT INTO moment(id, media_id, type, start_us, end_us, score, mark)"
        " VALUES (1, 1, 'toast', 0, 10, 0.5, 'must')"
    )
    con.execute("INSERT INTO audio_event VALUES (1, 0, 5, 'music', 0.3)")
    con.execute("PRAGMA user_version=2")
    con.commit()
    con.close()
    s = ProjectStore.open(root)
    assert s._con.execute("PRAGMA user_version").fetchone()[0] == 5
    assert [m.path for m in s.list_media()] == [Path("a.mp4")]
    assert len(s.audio_events(1)) == 1
    (m,) = s.moments(1)
    assert m.mark is Mark.MUST and m.reasons == () and m.preview is None
    assert m.quality_label is QualityLabel.CLEAR
    s.replace_audio(1, [SpeechRegion(0, 1, 0.5)], [])


def test_open_readonly_reads_moments(tmp_path):
    s = new_store(tmp_path)
    mid = add_video(s, tmp_path)
    s.replace_audio(mid, [SpeechRegion(0, 5, 0.9)], [AudioEvent(mid, 0, 5, AudioLabel.CHEER, 0.5)])
    s.replace_moments(mid, [moment(mid, 0, 5, MomentType.GIFTS)])
    s.close()
    ro = ProjectStore.open_readonly(s.root)
    assert len(ro.moments()) == 1 and ro.moment_counts() == {MomentType.GIFTS: 1}
    assert len(ro.speech_regions(mid)) == 1 and len(ro.audio_events(mid)) == 1
    ro.close()


def test_moment_ids_never_reused_and_excerpt_carried(tmp_path):
    s = new_store(tmp_path)
    mid = add_video(s, tmp_path)
    first = s.replace_moments(mid, [moment(mid, 0, 10), moment(mid, 20, 30)])
    s.set_mark(first[0], Mark.MUST)
    s._con.execute(
        "UPDATE moment SET transcript_excerpt='тост', user_text='u' WHERE id=?", (first[0],)
    )
    second = s.replace_moments(mid, [moment(mid, 0, 10), moment(mid, 50, 60)])
    assert not set(first) & set(second) and min(second) > max(first)
    row = s._con.execute(
        "SELECT mark, transcript_excerpt, user_text FROM moment WHERE id=?", (second[0],)
    ).fetchone()
    assert row == ("must", "тост", "u")


def test_migrated_v2_new_moment_ids_above_old_max(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    con = sqlite3.connect(root / "project.db")
    for name in ("0001_init.sql", "0002_analysis.sql"):
        sql = resources.files("toystudio.storage").joinpath("migrations", name)
        con.executescript(sql.read_text(encoding="utf-8"))
    con.execute("INSERT INTO project VALUES (1, 'Той', 'wedding', NULL, 'n', 'n', 2)")
    con.execute("INSERT INTO media(id, kind, path, size) VALUES (1, 'video', 'a.mp4', 5)")
    con.execute(
        "INSERT INTO moment(id, media_id, type, start_us, end_us, score, mark, transcript_excerpt)"
        " VALUES (7, 1, 'toast', 0, 10, 0.5, 'must', 'x')"
    )
    con.execute("PRAGMA user_version=2")
    con.commit()
    con.close()
    s = ProjectStore.open(root)
    assert [m.id for m in s.moments(1)] == [7]
    (new,) = s.replace_moments(1, [moment(1, 100, 200, MomentType.DANCE)])
    assert new > 7
