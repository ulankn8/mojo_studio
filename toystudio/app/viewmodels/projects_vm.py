"""View-model of the projects screen: listing, filtering, progress, delete/rename."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from toystudio.core.models import ProjectFormat, ToiKind
from toystudio.storage.app_store import AppStore
from toystudio.storage.project_store import ProjectStore

THUMB = Path("cache") / "thumbs" / "card.jpg"


@dataclass(frozen=True)
class ProjectItem:
    root: Path
    name: str
    kind: ToiKind
    event_date: date | None
    status: str
    percent: int | None
    eta_s: float | None
    duration_us: int
    preview_path: Path | None
    has_copies: bool = False
    format: ProjectFormat = ProjectFormat.TOI


_DB_FILES = ("project.db-wal", "project.db-shm", "project.db.bak", "project.db")


def _remove_project_dir(root: Path) -> None:
    """Delete everything but the db files first, so project.db marks an unfinished delete."""
    for child in root.iterdir():
        if child.name in _DB_FILES:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for name in _DB_FILES:  # project.db last
        (root / name).unlink(missing_ok=True)
    root.rmdir()


class ProjectsVM(QObject):
    changed = Signal()
    open_requested = Signal(Path, str)
    new_requested = Signal(list)

    def __init__(
        self,
        app_store: AppStore,
        is_busy: Callable[[Path], bool] = lambda root: False,
    ) -> None:
        super().__init__()
        self._app = app_store
        self.is_busy = is_busy
        self._all: list[ProjectItem] = []
        self._progress: dict[Path, tuple[int, float | None]] = {}
        self._text = ""
        self._status: str | None = None
        self.items: list[ProjectItem] = []
        self.last_error: str | None = None

    # ---- listing
    def reload(self) -> None:
        out: list[ProjectItem] = []
        for row in self._app.list_projects():
            s = ProjectStore.peek(row.root, row.name)  # read-only: a worker may be running
            pct, eta = self._progress.get(row.root, (None, None))
            thumb = row.root / THUMB
            out.append(
                ProjectItem(
                    root=row.root,
                    name=s.name,
                    kind=s.kind,
                    event_date=s.event_date,
                    status=row.status,
                    percent=pct if row.status == "analysis" else None,
                    eta_s=eta if row.status == "analysis" else None,
                    duration_us=s.duration_us,
                    preview_path=thumb if thumb.is_file() else None,
                    has_copies=s.has_copies,
                    format=s.format,
                )
            )
        self._all = out
        self._apply()

    def _apply(self) -> None:
        text = self._text.strip().casefold()
        self.items = [
            i
            for i in self._all
            if (not text or text in i.name.casefold())
            and (self._status is None or i.status == self._status)
        ]
        self.changed.emit()

    @property
    def total(self) -> int:
        return len(self._all)

    def item(self, root: Path) -> ProjectItem | None:
        return next((i for i in self._all if i.root == root.resolve()), None)

    def search(self, text: str) -> None:
        self._text = text
        self._apply()

    def filter_status(self, status: str | None) -> None:
        self._status = status
        self._apply()

    def set_progress(self, root: Path, percent: int, eta_s: float | None) -> None:
        root = root.resolve()
        self._progress[root] = (percent, eta_s)
        self._all = [
            replace(i, percent=percent, eta_s=eta_s) if i.root == root else i for i in self._all
        ]
        self._apply()

    # ---- actions
    def open(self, root: Path) -> None:
        item = self.item(root)
        s = ProjectStore.peek(root, item.name if item else "")
        self.open_requested.emit(root.resolve(), s.step)

    def request_new(self, files: list[Path]) -> None:
        self.new_requested.emit(files)

    def delete(self, root: Path) -> bool:
        """Remove the project folder and its list entry; never touches videos outside it."""
        self.last_error = None
        root = root.resolve()
        if self.is_busy(root):
            return False
        if (root / "project.db").is_file():
            try:
                _remove_project_dir(root)
            except OSError as e:  # locked file on Windows: keep the entry, report to the user
                self.last_error = str(e)
                self.reload()
                return False
        self._app.remove_project(root)
        self._progress.pop(root, None)
        self.reload()
        return True

    def rename(self, root: Path, name: str) -> bool:
        root = root.resolve()
        name = name.strip()
        if not name or self.is_busy(root):
            return False
        row = next((r for r in self._app.list_projects() if r.root == root), None)
        if row is None:
            return False
        if (root / "project.db").is_file():
            store = ProjectStore.open(root)  # not busy, so no worker can be mid-run
            try:
                store.rename(name)
            finally:
                store.close()
        self._app.register_project(root, name, row.status, datetime.now())
        self.reload()
        return True
