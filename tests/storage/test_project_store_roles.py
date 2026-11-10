"""Edit format of a project and track roles of its files (migration 0004)."""

import sqlite3
from datetime import date

from toystudio.core.models import MediaKind, MediaRole, ProjectFormat, ToiKind
from toystudio.storage.project_store import ProjectStore


def _new(tmp_path, fmt=ProjectFormat.WEDDING_FULL) -> ProjectStore:
    return ProjectStore.create(tmp_path / "p", "Свадьба", ToiKind.WEDDING, date(2026, 6, 14), fmt)


def test_format_saved_and_read_back(tmp_path):
    s = _new(tmp_path)
    s.close()
    again = ProjectStore.open(tmp_path / "p")
    try:
        assert again.format is ProjectFormat.WEDDING_FULL
        assert ProjectStore.peek(tmp_path / "p").format is ProjectFormat.WEDDING_FULL
    finally:
        again.close()


def test_default_format_is_toi(tmp_path):
    s = ProjectStore.create(tmp_path / "p", "Той", ToiKind.TOI, date(2026, 1, 1))
    try:
        assert s.format is ProjectFormat.TOI
    finally:
        s.close()


def test_roles_saved_with_matching_kind(tmp_path):
    s = _new(tmp_path)
    try:
        for i, role in enumerate(MediaRole):
            s.add_media(tmp_path / f"f{i}", role, 10 + i, False)
        rows = {m.role: m for m in s.list_media()}
        assert set(rows) == set(MediaRole)
        assert rows[MediaRole.SPEECH].kind is MediaKind.MUSIC
        assert rows[MediaRole.CUTAWAY].kind is MediaKind.VIDEO
    finally:
        s.close()


def test_bare_kind_gets_default_role(tmp_path):
    s = _new(tmp_path)
    try:
        s.add_media(tmp_path / "a.mp4", MediaKind.VIDEO, 1, False)
        s.add_media(tmp_path / "b.mp3", MediaKind.MUSIC, 2, False)
        assert [m.role for m in s.list_media()] == [MediaRole.MAIN, MediaRole.MUSIC]
    finally:
        s.close()


def test_analyzable_media_takes_videos_and_speech_not_music(tmp_path):
    s = _new(tmp_path)
    try:
        s.add_media(tmp_path / "pre.mp4", MediaRole.PRE, 1, False)
        s.add_media(tmp_path / "desk.wav", MediaRole.SPEECH, 2, False)
        s.add_media(tmp_path / "song.wav", MediaRole.MUSIC, 3, False)
        assert [m.path.name for m in s.analyzable_media()] == ["pre.mp4", "desk.wav"]
    finally:
        s.close()


def test_projects_before_roles_keep_working(tmp_path):
    """A v3 database (no format/role columns) migrates; old rows read as toi / main / music."""
    s = _new(tmp_path)
    s.add_media(tmp_path / "a.mp4", MediaKind.VIDEO, 1, False)
    s.add_media(tmp_path / "b.mp3", MediaKind.MUSIC, 2, False)
    s.close()
    con = sqlite3.connect(str(tmp_path / "p" / "project.db"))
    con.execute("UPDATE project SET format=NULL")
    con.execute("UPDATE media SET role=NULL")
    con.commit()
    con.close()
    again = ProjectStore.open(tmp_path / "p")
    try:
        assert again.format is ProjectFormat.TOI
        assert [m.role for m in again.list_media()] == [MediaRole.MAIN, MediaRole.MUSIC]
    finally:
        again.close()
