import dataclasses
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from tests.conftest import tl3
from toystudio.core.models import US_PER_S, AudioMode, Reason, ToiKind, Track
from toystudio.core.timeline import Segment, Timeline
from toystudio.storage.app_store import AppStore
from toystudio.storage.project_store import ProjectStore
from toystudio.storage.serde import timeline_from_json, timeline_to_json


def new_store(tmp_path: Path) -> ProjectStore:
    root = tmp_path / "Той Студия" / "Свадьба Айбек и Нурайым"
    return ProjectStore.create(root, "Свадьба Айбек и Нурайым", ToiKind.WEDDING, date(2026, 10, 12))


def test_store_cyrillic_path(tmp_path):
    root = tmp_path / "Той Студия" / "Свадьба Айбек и Нурайым"
    ProjectStore.create(root, "Свадьба Айбек и Нурайым", ToiKind.WEDDING, date(2026, 10, 12))
    assert ProjectStore.open(root).name == "Свадьба Айбек и Нурайым"
    for d in ("proxy", "audio", "frames", "thumbs", "wave", "export"):
        assert (root / "cache" / d).is_dir()


def test_timeline_serde_roundtrip():
    assert timeline_from_json(timeline_to_json(tl3())) == tl3()
    assert '"v": 1' in timeline_to_json(tl3())


def test_timeline_roundtrip(tmp_path):
    s = new_store(tmp_path)
    cut = s.create_cut("Клип 5 мин", "16:9", 300 * US_PER_S)
    s.save_timeline(cut, tl3())
    assert s.load_timeline(cut) == tl3()


def test_load_timeline_none_when_empty(tmp_path):
    s = new_store(tmp_path)
    assert s.load_timeline(s.create_cut("x", "16:9", 1)) is None


def test_snapshot_retention(tmp_path):
    s = new_store(tmp_path)
    cut = s.create_cut("x", "16:9", 1)
    seqs = [s.save_timeline(cut, tl3()) for _ in range(60)]
    assert seqs[-1] == 60
    assert s.snapshot_count(cut) == 50


def test_running_jobs_become_interrupted_on_open(tmp_path):
    s = new_store(tmp_path)
    s.set_job_state("j1", "running")
    s.set_job_state("j2", "done")
    s.close()
    again = ProjectStore.open(s.root)
    assert again.job_state("j1") == "interrupted"
    assert again.job_state("j2") == "done"


def test_user_version_and_wal(tmp_path):
    s = new_store(tmp_path)
    s.close()
    con = sqlite3.connect(str(s.root / "project.db"))
    assert con.execute("PRAGMA user_version").fetchone()[0] == 5
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "project",
        "media",
        "stage_run",
        "transcript_segment",
        "audio_event",
        "scene_cut",
        "moment",
        "cut",
        "task",
        "plan",
        "timeline_snapshot",
        "export_job",
        "job",
    } <= tables
    con.close()


def test_backup_only_when_migrating(tmp_path, monkeypatch):
    s = new_store(tmp_path)
    s.close()
    ProjectStore.open(s.root).close()
    assert not (s.root / "project.db.bak").exists()

    from toystudio.storage import db

    real = db._migrations
    monkeypatch.setattr(
        db, "_migrations", lambda pkg: [*real(pkg), (6, "CREATE TABLE extra (x INTEGER);")]
    )
    ProjectStore.open(s.root).close()
    assert (s.root / "project.db.bak").exists()
    con = sqlite3.connect(str(s.root / "project.db"))
    assert con.execute("PRAGMA user_version").fetchone()[0] == 6
    con.close()


def test_list_projects_analysis_first(tmp_path):
    a = AppStore(tmp_path / "app.db")
    a.register_project(Path("A"), "A", "ready", datetime(2026, 11, 5))
    a.register_project(Path("B"), "B", "analysis", datetime(2026, 11, 1))
    a.register_project(Path("C"), "C", "done", datetime(2026, 11, 6))
    rows = a.list_projects()
    assert [r.name for r in rows] == ["B", "C", "A"]
    assert rows[0].root == Path("B").resolve() and rows[0].updated_at == datetime(2026, 11, 1)


def test_register_project_upserts(tmp_path):
    a = AppStore(tmp_path / "app.db")
    a.register_project(Path("A"), "A", "analysis", datetime(2026, 11, 1))
    a.register_project(Path("A"), "A2", "done", datetime(2026, 11, 2))
    rows = a.list_projects()
    assert len(rows) == 1 and rows[0].name == "A2" and rows[0].status == "done"


def test_register_project_normalizes_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = AppStore(tmp_path / "app.db")
    a.register_project(Path("A"), "A", "ready", datetime(2026, 11, 1))
    a.register_project(tmp_path / "x" / ".." / "A", "A", "ready", datetime(2026, 11, 2))
    assert len(a.list_projects()) == 1


def test_create_failure_closes_connection(tmp_path, monkeypatch):
    s = new_store(tmp_path)
    s.close()
    opened = []
    from toystudio.storage import project_store

    real = project_store.connect

    def spy(path):
        con = real(path)
        opened.append(con)
        return con

    monkeypatch.setattr(project_store, "connect", spy)
    with pytest.raises(sqlite3.IntegrityError):
        ProjectStore.create(s.root, "dup", ToiKind.TOI, date(2026, 1, 1))
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_backup_is_atomic_and_leaves_no_tmp(tmp_path, monkeypatch):
    s = new_store(tmp_path)
    s.close()
    from toystudio.storage import db

    real = db._migrations
    monkeypatch.setattr(db, "_migrations", lambda pkg: [*real(pkg), (6, "SELECT 1;")])
    ProjectStore.open(s.root).close()
    assert not (s.root / "project.db.bak.tmp").exists()
    bak = sqlite3.connect(str(s.root / "project.db.bak"))
    assert bak.execute("SELECT name FROM project").fetchone()[0] == "Свадьба Айбек и Нурайым"
    bak.close()


def test_serde_roundtrip_all_fields_non_default():
    video = Segment(
        id="v1",
        track=Track.VIDEO,
        media_id="m9",
        src_in=1_000,
        src_out=9_000,
        moment_id="mom",
        bound_in=0,
        bound_out=10_000,
        locked=True,
        manual=True,
        audio_mode=AudioMode.DUCK,
        crop_x=0.25,
        reasons=(Reason("r1", "причина", "себеп"),),
        text="Тост",
        anchor_id="a1",
    )
    music = Segment(
        id="mu1",
        track=Track.MUSIC,
        media_id="song",
        src_in=0,
        src_out=5_000,
        moment_id=None,
        bound_in=0,
        bound_out=5_000,
        audio_mode=AudioMode.MUSIC,
    )
    tl = Timeline(video=(video,), music=(music,))
    assert timeline_from_json(timeline_to_json(tl)) == tl
    obj = json.loads(timeline_to_json(tl))
    expected = {f.name for f in dataclasses.fields(Segment)}
    assert set(obj["video"][0]) == expected
    assert set(obj["music"][0]) == expected


def test_update_details_changes_kind_and_date(tmp_path):
    root = tmp_path / "Той"
    s = ProjectStore.create(root, "Той", ToiKind.TOI, date(2026, 1, 2))
    s.update_details(ToiKind.JUBILEE, date(2026, 3, 4))
    s.close()
    peeked = ProjectStore.peek(root)
    assert (peeked.kind, peeked.event_date) == (ToiKind.JUBILEE, date(2026, 3, 4))


def test_open_readonly_does_not_change_running_stage(tmp_path):
    from toystudio.core.models import MediaKind, StageName, StageState

    store = new_store(tmp_path)
    mid, _ = store.add_media(tmp_path / "a.mp4", MediaKind.VIDEO, 10, False)
    store.set_stage(mid, StageName.PROXY, StageState.RUNNING)
    ro = ProjectStore.open_readonly(store.root)
    assert ro.stage(mid, StageName.PROXY).state is StageState.RUNNING
    assert ro.name == "Свадьба Айбек и Нурайым"
    with pytest.raises(sqlite3.OperationalError):
        ro.set_stage(mid, StageName.PROXY, StageState.DONE)
    ro.close()
    assert store.stage(mid, StageName.PROXY).state is StageState.RUNNING
    with pytest.raises(FileNotFoundError):
        ProjectStore.open_readonly(tmp_path / "nope")
