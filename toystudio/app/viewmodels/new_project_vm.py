"""View-model of screen 2: project creation, file import, disk check, start of analysis."""

from __future__ import annotations

import contextlib
import ntpath
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from toystudio.app.widgets.drop_zone import FileRow, RowState
from toystudio.core.budget import needed_bytes
from toystudio.core.eta import DEFAULT_X_RT, over_target, profile_key, project_remaining_s
from toystudio.core.models import (
    FORMAT_ROLES,
    US_PER_S,
    MediaKind,
    MediaRole,
    MediaRow,
    MediaStatus,
    ProjectFormat,
    ToiKind,
    format_toi_kind,
)
from toystudio.jobs.launch import stop_file_for, worker_argv
from toystudio.jobs.manager import JobManager
from toystudio.storage.app_store import AppStore
from toystudio.storage.fs import free_bytes, is_removable
from toystudio.storage.project_store import ProjectStore

MEDIA_DIR = "media"
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)}
_RESERVED |= {f"LPT{i}" for i in range(1, 10)}
_BAD = (MediaStatus.CORRUPT, MediaStatus.MISSING)


@dataclass(frozen=True)
class DiskCheck:
    free: int
    needed: int
    drive: str  # "D:" on Windows, "" elsewhere
    ok: bool

    @property
    def shortage(self) -> int:
        return max(0, self.needed - self.free)


def drive_of(path: Path, platform: str = sys.platform) -> str:
    return ntpath.splitdrive(str(path))[0] if platform == "win32" else ""


def sanitize_folder_name(name: str) -> str:
    clean = _INVALID.sub("", name).strip().rstrip(". ").strip()
    if clean.split(".")[0].upper() in _RESERVED:
        clean += "_"
    return clean


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _creation_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


class NewProjectVM(QObject):
    rows_changed = Signal(object, list)
    disk_changed = Signal(object)
    banner = Signal(object)  # "disk" | "corrupt" | None
    can_start_changed = Signal(bool)
    warning_4k = Signal(bool)
    analysis_started = Signal(Path)
    date_changed = Signal(object)
    copy_changed = Signal(bool)

    def __init__(
        self,
        app_store: AppStore,
        jobs: JobManager,
        projects_dir: Path,
        fmt: ProjectFormat = ProjectFormat.TOI,
    ) -> None:
        super().__init__()
        self.format = fmt
        self._app = app_store
        self._jobs = jobs
        self._projects_dir = projects_dir
        self._store: ProjectStore | None = None
        self.root: Path | None = None
        self._name = ""
        self._kind = ToiKind.WEDDING
        self._date = date.today()
        self._date_touched = False
        self._copy = False
        self.removable_hint = False
        self._status = "files"
        self._import_active = False
        self._rerun = False
        self._copy_pct: dict[int, int] = {}
        self._excluded: set[int] = set()
        self._dups: dict[MediaRole, list[FileRow]] = {r: [] for r in MediaRole}
        self._dup_n = 0
        self._media: list[MediaRow] = []
        self._rows: dict[MediaRole, list[FileRow]] = {r: [] for r in MediaRole}
        self._disk = DiskCheck(0, 0, "", True)
        self._can_start = False
        self.banner_key: str | None = None
        self.import_failure: tuple[str, str] | None = None
        self.corrupt_name: str | None = None
        self.warning_4k_active = False
        self.eta_4k_s = 0.0
        self._queued: list[tuple[MediaRole, list[Path]]] = []
        self._closed = False
        jobs.event.connect(self._on_event)
        jobs.finished.connect(self._on_job_end)
        jobs.failed.connect(self._on_job_failed)

    # ---- details
    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value
        clean = value.strip()
        if self._store is not None and self.root is not None and clean:
            self._store.rename(clean)
            self._app.register_project(self.root, clean, self._status, datetime.now())

    @property
    def kind(self) -> ToiKind:
        return self._kind

    @kind.setter
    def kind(self, value: ToiKind) -> None:
        self._kind = value
        self._save_details()

    @property
    def event_date(self) -> date:
        return self._date

    @event_date.setter
    def event_date(self, value: date) -> None:
        self._date_touched = True
        self._date = value
        self._save_details()

    @property
    def copy(self) -> bool:
        return self._copy

    @copy.setter
    def copy(self, value: bool) -> None:
        self._set_copy(value)

    def _save_details(self) -> None:
        if self._store is not None:
            self._store.update_details(self._stored_kind, self._date)

    def _set_copy(self, value: bool, start: bool = True) -> bool:
        """Switch copy mode; returns True when existing rows were flipped (an import is due)."""
        changed = value != self._copy
        self._copy = value
        if not value:
            self.removable_hint = False
        flipped = False
        if changed:
            self.copy_changed.emit(value)
            flipped = self._apply_copy_to_rows()
            if flipped and start:
                self._start_import()
        self._refresh()
        return flipped

    def _apply_copy_to_rows(self) -> bool:
        """Rows not yet inside <root>/media follow the toggle; finished copies stay."""
        if self._store is None or self.root is None:
            return False
        media_dir = self.root / MEDIA_DIR
        changed = False
        for m in self._store.list_media():
            if (
                m.status is MediaStatus.OK
                and m.copied != self._copy
                and not _inside(m.path, media_dir)
            ):
                self._store.set_media_path(m.id, m.path, self._copy)
                changed = True
        return changed

    def set_projects_dir(self, path: Path) -> bool:
        """Pick another parent folder; possible only until the project folder exists."""
        if self._store is not None:
            return False
        self._projects_dir = path
        self._refresh()
        return True

    @property
    def _stored_kind(self) -> ToiKind:
        return format_toi_kind(self.format, self._kind)

    @property
    def roles(self) -> tuple[MediaRole, ...]:
        """Tracks shown on screen 2, in order."""
        return FORMAT_ROLES[self.format]

    def role_for(self, track: MediaRole | MediaKind) -> MediaRole:
        """A bare kind (a folder dropped on the projects screen) goes to the first such track."""
        if isinstance(track, MediaRole):
            return track
        return next((r for r in self.roles if r.kind is track), MediaRole.default_for(track))

    @property
    def projects_dir(self) -> Path:
        return self._projects_dir

    # ---- files
    def add_files(self, track: MediaRole | MediaKind, paths: list[Path]) -> None:
        role = self.role_for(track)
        if self.root is not None and self._store is None:  # attached read-only: a worker runs
            self._queued.append((role, list(paths)))
            return
        self._dups = {r: [] for r in MediaRole}
        files = [p for p in paths if p.is_file()]
        if not files:
            return
        added = False
        if any(is_removable(p) for p in files):
            self.removable_hint = True
            if self._set_copy(True, start=False):
                added = True
        store = self._ensure_project()
        for p in files:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            _, created = store.add_media(p, role, size, self._copy)
            if created:
                added = True
            else:
                self._dup_n += 1
                row = FileRow(f"dup:{self._dup_n}", p.name, size, None, "duplicate", None)
                self._dups[role].append(row)
        self._refresh()
        if added:
            self._start_import()

    def remove_file(self, media_id: int) -> None:
        if self._store is None:
            return
        old = next((m for m in self._store.list_media() if m.id == media_id), None)
        self._store.remove_media(media_id)
        self._excluded.discard(media_id)
        self._copy_pct.pop(media_id, None)
        if old is not None and self.root is not None and _inside(old.path, self.root / MEDIA_DIR):
            with contextlib.suppress(OSError):
                old.path.unlink()
        self._refresh()

    def replace_file(self, media_id: int, path: Path) -> None:
        if self._store is None:
            return
        old = next((m for m in self._store.list_media() if m.id == media_id), None)
        role = old.role if old is not None else self.role_for(MediaKind.VIDEO)
        self.remove_file(media_id)
        self.add_files(role, [path])

    def exclude_bad(self) -> None:
        """«Продолжить без него»: bad files stay in the list but no longer block anything."""
        self._excluded.update(m.id for m in self._media if m.status in _BAD)
        self._refresh()

    def first_bad_id(self) -> int | None:
        return next((m.id for m in self._bad_media()), None)

    def rows(self, track: MediaRole | MediaKind) -> list[FileRow]:
        return list(self._rows[self.role_for(track)])

    def recheck(self) -> None:
        self._refresh(force=True)

    # ---- project
    def _folder_for(self) -> Path:
        base = sanitize_folder_name(self._name)
        if not base:
            n = 1
            while (self._projects_dir / f"Новый проект {n}").exists():
                n += 1
            return self._projects_dir / f"Новый проект {n}"
        folder, n = self._projects_dir / base, 2
        while folder.exists():
            folder = self._projects_dir / f"{base} ({n})"
            n += 1
        return folder

    def _ensure_project(self) -> ProjectStore:
        if self._store is not None:
            return self._store
        folder = self._folder_for()
        name = self._name.strip() or folder.name
        self._store = ProjectStore.create(folder, name, self._stored_kind, self._date, self.format)
        self.root = folder.resolve()  # job ids and the project list use resolved roots
        self._app.register_project(folder, name, self._status, datetime.now())
        return self._store

    def attach(self, root: Path) -> None:
        """Show an existing project on screen 2.

        ProjectStore.open() flips a running worker's stages to interrupted (rulings R11/R17),
        so while an import or analysis of this project runs the VM only reads project.db
        (open_readonly on every refresh) and opens it for writing once the job ends.
        """
        if self.root is not None:
            raise RuntimeError("the view-model already has a project")
        summary = ProjectStore.peek(root)
        self.root = root
        self._name, self._kind, self.format = summary.name, summary.kind, summary.format
        self._date = summary.event_date or self._date
        self._date_touched = True  # the saved date wins over the clip's creation time
        self._copy = summary.has_copies
        self._status = summary.step
        self._import_active = self._jobs.is_active(self._import_id())
        self._open_if_idle()
        if self._store is not None and self._import_due():
            self._start_import()  # an import cut off by a close or a crash: run it again
        self._refresh()

    def _import_due(self) -> bool:
        """Rows the import job would still work on (same rule as import_job.run)."""
        assert self._store is not None and self.root is not None
        media_dir = self.root / MEDIA_DIR
        for m in self._store.list_media():
            if m.status is MediaStatus.MISSING and m.path.is_file():
                return True  # the drive is back: import flips it to OK
            if m.status is MediaStatus.OK and (
                m.info is None or (m.copied and not _inside(m.path, media_dir))
            ):
                return True
        return False

    def _worker_running(self) -> bool:
        """A worker of this project runs or is queued (it will write project.db)."""
        return self._jobs.is_active(self._import_id()) or self._jobs.is_active(self._analyze_id())

    def _open_if_idle(self) -> None:
        if self._closed or self._store is not None or self.root is None:
            return
        if self._worker_running():
            return
        self._store = ProjectStore.open(self.root)
        queued, self._queued = self._queued, []
        for kind, paths in queued:
            self.add_files(kind, paths)

    def _read_media(self) -> list[MediaRow]:
        if self._store is not None:
            return self._store.list_media()
        if self.root is None:
            return []
        try:
            ro = ProjectStore.open_readonly(self.root)
        except (OSError, sqlite3.Error):
            return self._media
        try:
            return ro.list_media()
        except sqlite3.Error:
            return self._media
        finally:
            ro.close()

    def close(self) -> None:
        self._closed = True
        if self._store is not None:
            self._store.close()
            self._store = None

    # ---- jobs
    def _import_id(self) -> str:
        return f"import:{self.root}"

    def _analyze_id(self) -> str:
        return f"analyze:{self.root}"

    def _start_import(self) -> None:
        if self.root is None:
            return
        if self._import_active:
            self._rerun = True
            return
        stop = stop_file_for(self.root, "import")
        stop.unlink(missing_ok=True)
        argv = worker_argv(
            "import", self.root, self._import_id(), app_db=self._app.path, stop_file=stop
        )
        self.import_failure = None
        self._import_active = True
        try:
            self._jobs.start(self._import_id(), argv)
        except ValueError:  # the manager still has it: run again once it ends
            self._rerun = True
        self._refresh()

    def _on_event(self, job_id: str, ev: object) -> None:
        if self.root is None or job_id != self._import_id():
            return
        kind = getattr(ev, "kind", "")
        payload = getattr(ev, "payload", {})
        mid = int(payload.get("media_id", 0))
        if kind == "copy":
            total = int(payload.get("total_bytes", 0))
            done = int(payload.get("done_bytes", 0))
            pct = int(done * 100 / total) if total else 100
            if pct >= 100:
                self._copy_pct.pop(mid, None)
            elif self._copy_pct.get(mid) == pct:
                return
            else:
                self._copy_pct[mid] = pct
        elif kind == "media":
            self._copy_pct.pop(mid, None)
        self._refresh()

    def _on_job_end(self, job_id: str) -> None:
        if self.root is None or job_id not in (self._import_id(), self._analyze_id()):
            return
        self._open_if_idle()
        if job_id != self._import_id():
            self._refresh()
            return
        self._import_active = False
        self._copy_pct.clear()
        if self._rerun:
            self._rerun = False
            self._start_import()
        else:
            self._refresh()

    def _on_job_failed(self, job_id: str, code: str, detail: str) -> None:
        if self.root is None or job_id not in (self._import_id(), self._analyze_id()):
            return
        if job_id == self._import_id() and code != "cancelled":
            self.import_failure = (code, detail)
        self._on_job_end(job_id)

    def retry_import(self) -> None:
        self._start_import()

    def start_analysis(self) -> None:
        if not self._can_start or self.root is None:
            return
        job_id = self._analyze_id()
        stop = stop_file_for(self.root, "analyze")
        stop.unlink(missing_ok=True)
        argv = worker_argv("analyze", self.root, job_id, app_db=self._app.path, stop_file=stop)
        self._jobs.start(job_id, argv)
        self._status = "analysis"
        self._app.set_status(self.root, "analysis", datetime.now())
        self.analysis_started.emit(self.root)

    # ---- derived state
    def _bad_media(self) -> list[MediaRow]:
        return [m for m in self._media if m.status in _BAD and m.id not in self._excluded]

    def _row_for(self, m: MediaRow) -> FileRow:
        state: RowState
        percent = None
        if m.status is MediaStatus.CORRUPT:
            state = "corrupt"
        elif m.status is MediaStatus.MISSING:
            state = "missing"
        elif m.info is None:
            percent = self._copy_pct.get(m.id)
            state = "copying" if percent is not None else "checking"
        else:
            state = "ready"
        duration = m.info.duration_us if m.info else None
        return FileRow(str(m.id), m.path.name, m.size, duration, state, percent)

    def _pending_copy_bytes(self) -> int:
        media_dir = None if self.root is None else self.root / MEDIA_DIR
        return sum(
            m.size
            for m in self._media
            if m.copied
            and m.id not in self._excluded
            and not (media_dir and _inside(m.path, media_dir))
        )

    def disk_check(self) -> DiskCheck:
        live = [m for m in self._media if m.status not in _BAD and m.id not in self._excluded]
        items = [(m.size, m.info.duration_us if m.info else 0) for m in live]
        needed = needed_bytes(items, False) + self._pending_copy_bytes()
        target = self._projects_dir if self._copy or self.root is None else self.root
        free = free_bytes(target)
        return DiskCheck(free, needed, drive_of(target), free >= needed)

    def _update_default_date(self) -> None:
        if self._date_touched:
            return
        for m in self._media:
            if m.kind is MediaKind.VIDEO and m.info is not None and m.status is MediaStatus.OK:
                d = _creation_date(m.info.creation_time)
                if d is not None:
                    if d != self._date:
                        self._date = d
                        self._save_details()
                        self.date_changed.emit(d)
                    return

    def _refresh(self, force: bool = False) -> None:
        self._media = self._read_media()
        self._update_default_date()
        for role in MediaRole:
            rows = [self._row_for(m) for m in self._media if m.role is role] + self._dups[role]
            if force or rows != self._rows[role]:
                self._rows[role] = rows
                self.rows_changed.emit(role, list(rows))
        disk = self.disk_check()
        if force or disk != self._disk:
            self._disk = disk
            self.disk_changed.emit(disk)
        bad = self._bad_media()
        self.corrupt_name = bad[0].path.name if bad else None
        key = (
            "disk"
            if not disk.ok
            else "import_failed"
            if self.import_failure is not None
            else "corrupt"
            if bad
            else None
        )
        if key != self.banner_key or force:
            self.banner_key = key
            self.banner.emit(key)
        ready = [
            m
            for m in self._media
            if m.kind is MediaKind.VIDEO
            and m.status is MediaStatus.OK
            and m.info is not None
            and m.id not in self._excluded
        ]
        heavy = [(m.info, m.info.duration_us / US_PER_S) for m in ready if m.info is not None]
        warn = over_target(heavy)
        self.eta_4k_s = project_remaining_s(
            [(profile_key(i), s) for i, s in heavy if profile_key(i) == "4k_hevc10"], DEFAULT_X_RT
        )
        if warn != self.warning_4k_active:
            self.warning_4k_active = warn
            self.warning_4k.emit(warn)
        unchecked = any(
            m.analyzable
            and m.status is MediaStatus.OK
            and m.info is None
            and m.id not in self._excluded
            for m in self._media
        )
        can = (
            self._store is not None
            and bool(ready)
            and not unchecked
            and not self._import_active
            and not self._rerun
            and disk.ok
        )
        if can != self._can_start:
            self._can_start = can
            self.can_start_changed.emit(can)

    @property
    def can_start(self) -> bool:
        return self._can_start
