import sqlite3
from datetime import date
from importlib import resources
from pathlib import Path

from toystudio.core.models import (
    STAGE_ORDER,
    MediaInfo,
    MediaKind,
    MediaStatus,
    QualityInterval,
    QualityLabel,
    StageName,
    StageState,
    ToiKind,
)
from toystudio.storage.app_store import AppStore
from toystudio.storage.project_store import ProjectStore


def new_store(tmp_path: Path) -> ProjectStore:
    return ProjectStore.create(tmp_path / "p", "Той", ToiKind.WEDDING, date(2026, 10, 12))


def add_video(s: ProjectStore, tmp_path: Path, name: str = "IMG_0001.MP4") -> int:
    f = tmp_path / name
    f.write_bytes(b"x" * (10 + len(name)))
    return s.add_media(f, MediaKind.VIDEO, f.stat().st_size, copied=False)[0]


def finish_all(s: ProjectStore, mid: int) -> None:
    for st in STAGE_ORDER:
        s.set_stage(mid, st, StageState.DONE)


def test_add_media_duplicate_returns_existing(tmp_path):
    s = new_store(tmp_path)
    f = tmp_path / "IMG_0412.MP4"
    f.write_bytes(b"x" * 10)
    a = s.add_media(f, MediaKind.VIDEO, 10, copied=False)
    assert a[1] is True
    assert s.add_media(f, MediaKind.VIDEO, 10, copied=False) == (a[0], False)
    g = tmp_path / "copy" / "IMG_0412.MP4"
    g.parent.mkdir()
    g.write_bytes(b"x" * 10)
    assert s.add_media(g, MediaKind.VIDEO, 10, copied=False) == (a[0], False)


def test_stage_roundtrip_and_running_becomes_interrupted(tmp_path):
    s = new_store(tmp_path)
    mid = add_video(s, tmp_path)
    s.set_stage(mid, StageName.PROXY, StageState.RUNNING, progress=0.4)
    s.close()
    s = ProjectStore.open(tmp_path / "p")
    assert s.stage(mid, StageName.PROXY).state is StageState.INTERRUPTED


def test_step_progression(tmp_path):
    s = new_store(tmp_path)
    assert s.step() == "files"
    mid = add_video(s, tmp_path)
    assert s.step() == "files"  # ruling R21: analysis never started
    s.set_stage(mid, StageName.PROBE, StageState.PENDING)
    assert s.step() == "analysis"
    for st in STAGE_ORDER:
        s.set_stage(
            mid,
            st,
            StageState.SKIPPED if st in (StageName.SPEECH, StageName.MOMENTS) else StageState.DONE,
        )
    s.set_stage(0, StageName.SYNC, StageState.DONE)
    assert s.step() == "analysis_done"


def test_corrupt_media_does_not_block_step(tmp_path):
    s = new_store(tmp_path)
    bad = add_video(s, tmp_path, "bad.MP4")
    good = add_video(s, tmp_path, "good.MP4")
    s.set_media_status(bad, MediaStatus.CORRUPT)
    assert s.step() == "files"
    s.set_stage(bad, StageName.PROBE, StageState.FAILED, error_code="corrupt")
    assert s.step() == "analysis"  # analysis started: the corrupt file was tried
    finish_all(s, good)
    s.set_stage(0, StageName.SYNC, StageState.DONE)
    assert s.step() == "analysis_done"


def test_migration_from_v1_keeps_data(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    sql = resources.files("toystudio.storage").joinpath("migrations/0001_init.sql")
    con = sqlite3.connect(root / "project.db")
    con.executescript(sql.read_text(encoding="utf-8"))
    con.execute("INSERT INTO project VALUES (1, 'Той', 'wedding', NULL, 'n', 'n', 1)")
    con.execute("INSERT INTO media(id, kind, path, size) VALUES (1, 'video', 'a.mp4', 5)")
    con.execute(
        "INSERT INTO stage_run(media_id, stage, state, progress, version)"
        " VALUES (1, 'probe', 'done', 1.0, 1)"
    )
    con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()
    s = ProjectStore.open(root)
    assert s._con.execute("PRAGMA user_version").fetchone()[0] == 5
    assert [m.path for m in s.list_media()] == [Path("a.mp4")]
    run = s.stage(1, StageName.PROBE)
    assert run.state is StageState.DONE and run.progress == 1.0
    s.set_stage(1, StageName.PROXY, StageState.INTERRUPTED)


def test_store_cyrillic_media_path(tmp_path):
    s = new_store(tmp_path)
    p = tmp_path / "Той Студия" / "Свадьба Айбек" / "IMG 1.MP4"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    mid, _ = s.add_media(p, MediaKind.VIDEO, 1, copied=True)
    row = s.list_media()[0]
    assert row.id == mid and row.path == p and row.copied is True


def test_media_info_roundtrip_filter_and_remove(tmp_path):
    s = new_store(tmp_path)
    mid = add_video(s, tmp_path)
    assert s.list_media()[0].info is None
    info = MediaInfo(5_000_000, 1920, 1080, 90, 29.97, "h264", False, True, True, 8_000_000, None)
    s.set_media_info(mid, info)
    assert s.list_media(MediaKind.VIDEO)[0].info == info
    assert s.list_media(MediaKind.MUSIC) == []
    s.set_media_path(mid, tmp_path / "p" / "media" / "a.mp4", copied=True)
    assert s.list_media()[0].copied is True
    s.set_sync(mid, -1200, 0.8)
    s.replace_scene_cuts(mid, [1, 2], [0.5, 0.6])
    s.replace_quality(mid, [QualityInterval(mid, 0, 5, QualityLabel.DARK)])
    s.set_stage(mid, StageName.PROBE, StageState.DONE)
    s.set_stage(0, StageName.SYNC, StageState.DONE)
    s.remove_media(mid)
    assert s.list_media() == [] and s.scene_cut_count() == 0
    assert s.quality_intervals(mid) == []
    assert [r.media_id for r in s.stages()] == [0]


def test_scene_cuts_and_quality_replace(tmp_path):
    s = new_store(tmp_path)
    a, b = add_video(s, tmp_path, "a.MP4"), add_video(s, tmp_path, "b.MP4")
    s.replace_scene_cuts(a, [1, 2, 3], [0.1, 0.2, 0.3])
    s.replace_scene_cuts(b, [9], [0.9])
    s.replace_scene_cuts(a, [4], [0.4])
    assert s.scene_cut_count(a) == 1 and s.scene_cut_count() == 2
    ivs = [
        QualityInterval(a, 0, 10, QualityLabel.SHAKY),
        QualityInterval(a, 10, 20, QualityLabel.CLEAR),
    ]
    s.replace_quality(a, ivs)
    assert s.quality_intervals(a) == [ivs[0]]


def test_scene_cuts_reader_sorted_per_media(tmp_path):
    s = new_store(tmp_path)
    a, b = add_video(s, tmp_path, "a.MP4"), add_video(s, tmp_path, "b.MP4")
    s.replace_scene_cuts(a, [30, 10, 20], [0.1, 0.2, 0.3])
    s.replace_scene_cuts(b, [5], [0.5])
    assert s.scene_cuts(a) == [10, 20, 30]
    assert s.scene_cuts(b) == [5]
    assert s.scene_cuts(999) == []


def test_app_store_speed_and_status(tmp_path):
    a = AppStore(tmp_path / "app.db")
    assert a.speed("proxy", "cpu") is None
    a.set_speed("proxy", "cpu", 2.5)
    a.set_speed("proxy", "cpu", 3.0)
    assert a.speed("proxy", "cpu") == 3.0
    from datetime import datetime

    root = tmp_path / "p"
    a.register_project(root, "n", "files", datetime(2026, 1, 1))
    a.set_status(root, "analysis", datetime(2026, 1, 2))
    assert a.list_projects()[0].status == "analysis"


def test_failed_stage_counts_as_finished(tmp_path):
    # ruling R9: a file with a FAILED stage is finished for the wizard (retry is on rerun)
    s = new_store(tmp_path)
    failed = add_video(s, tmp_path, "failed.MP4")
    good = add_video(s, tmp_path, "good.MP4")
    s.set_stage(failed, StageName.PROBE, StageState.DONE)
    s.set_stage(failed, StageName.PROXY, StageState.FAILED, error_code="ffmpeg")
    finish_all(s, good)
    s.set_stage(0, StageName.SYNC, StageState.DONE)
    assert s.step() == "analysis_done"
    s.set_stage(failed, StageName.PROXY, StageState.INTERRUPTED)
    assert s.step() == "analysis"


def test_failed_sync_counts_as_finished(tmp_path):
    s = new_store(tmp_path)
    mid = add_video(s, tmp_path)
    finish_all(s, mid)
    s.set_stage(0, StageName.SYNC, StageState.FAILED, error_code="error")
    assert s.step() == "analysis_done"


def test_peek_with_media_but_no_stages_is_files_step(tmp_path):
    s = new_store(tmp_path)
    add_video(s, tmp_path)
    root = s.root
    s.close()
    assert ProjectStore.peek(root).step == "files"


def test_peek_returns_summary_and_does_not_mutate(tmp_path):
    s = new_store(tmp_path)
    a = add_video(s, tmp_path, "a.MP4")
    b = add_video(s, tmp_path, "b.MP4")
    s.set_media_info(
        a, MediaInfo(5_000_000, 1920, 1080, 0, 25.0, "h264", False, True, True, 1, None)
    )
    s.set_media_info(
        b, MediaInfo(7_000_000, 1920, 1080, 0, 25.0, "h264", False, True, True, 1, None)
    )
    s.set_stage(a, StageName.PROBE, StageState.RUNNING)
    root = s.root
    s.close()
    p = ProjectStore.peek(root)
    assert (p.name, p.kind, p.event_date) == ("Той", ToiKind.WEDDING, date(2026, 10, 12))
    assert p.duration_us == 12_000_000
    assert p.step == "analysis"
    assert not p.has_copies
    con = sqlite3.connect(root / "project.db")
    state = con.execute("SELECT state FROM stage_run WHERE media_id=?", (a,)).fetchone()[0]
    con.close()
    assert state == StageState.RUNNING.value


def test_peek_empty_project_is_files_step(tmp_path):
    s = new_store(tmp_path)
    root = s.root
    s.close()
    p = ProjectStore.peek(root)
    assert (p.step, p.duration_us) == ("files", 0)


def test_peek_old_schema_falls_back(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    con = sqlite3.connect(root / "project.db")
    con.execute("PRAGMA user_version=1")
    con.execute("CREATE TABLE project (id INTEGER)")
    con.commit()
    con.close()
    p = ProjectStore.peek(root, "Старый")
    assert (p.name, p.step, p.duration_us) == ("Старый", "files", 0)
    con = sqlite3.connect(root / "project.db")
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
    con.close()


def test_peek_missing_db_falls_back(tmp_path):
    assert ProjectStore.peek(tmp_path / "nope", "X").name == "X"


def test_rename_and_remove_project(tmp_path):
    s = new_store(tmp_path)
    s.rename("Новое")
    assert s.name == "Новое"
    app = AppStore(tmp_path / "app.db")
    from datetime import datetime

    app.register_project(s.root, "Новое", "files", datetime(2026, 1, 1))
    app.remove_project(s.root)
    assert app.list_projects() == []


def test_readonly_uri_cyrillic_path(tmp_path):
    from toystudio.storage.project_store import readonly_uri

    root = tmp_path / "Той Студия" / "Свадьба Айбек #1"
    s = ProjectStore.create(root, "Той", ToiKind.WEDDING, date(2026, 10, 12))
    add_video(s, tmp_path)
    s.close()
    uri = readonly_uri(root / "project.db")
    assert uri.startswith("file:") and uri.endswith("?mode=ro")
    assert "Той" not in uri and " " not in uri and "#" not in uri  # percent-encoded
    ro = ProjectStore.open_readonly(root)
    try:
        assert ro.name == "Той" and len(ro.list_media()) == 1
    finally:
        ro.close()
    assert ProjectStore.peek(root).name == "Той"


def test_readonly_uri_windows_drive_and_unc(monkeypatch):
    import nturl2path

    from toystudio.storage import project_store

    win = nturl2path.pathname2url
    monkeypatch.setattr(project_store.os.path, "abspath", str)  # Windows paths: already absolute
    assert project_store.readonly_uri(r"Z:\Той\project.db", win) == (
        "file:///Z:/%D0%A2%D0%BE%D0%B9/project.db?mode=ro"
    )
    assert project_store.readonly_uri(r"\\server\share\p\project.db", win) == (
        "file:////server/share/p/project.db?mode=ro"
    )


def test_step_back_to_files_when_all_videos_removed_after_start(tmp_path):
    s = new_store(tmp_path)
    mid = add_video(s, tmp_path)
    finish_all(s, mid)
    s.set_stage(0, StageName.SYNC, StageState.DONE)
    assert s.step() == "analysis_done"
    s.remove_media(mid)  # the project-level SYNC row survives
    assert s.step() == "files"


def test_step_stays_in_analysis_while_a_video_is_missing(tmp_path):
    s = new_store(tmp_path)
    a = add_video(s, tmp_path, "A.MP4")
    b = add_video(s, tmp_path, "B.MP4")
    finish_all(s, a)
    s.set_stage(b, StageName.PROBE, StageState.PENDING)
    s.set_stage(0, StageName.SYNC, StageState.DONE)
    s.set_media_status(b, MediaStatus.MISSING)
    assert s.step() == "analysis"
