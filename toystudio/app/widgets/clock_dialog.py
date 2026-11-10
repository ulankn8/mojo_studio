"""«Сверить часы камер»: the user picks one clip of a camera and one of the reference camera
shot at the same time; the difference corrects the camera's whole clock."""

from __future__ import annotations

import contextlib
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from toystudio.app.theme.apply import set_variant
from toystudio.core.clock import offset_from_pair, shot_time
from toystudio.core.models import US_PER_S, MediaRow
from toystudio.engine.analysis.single_pass import outputs_for
from toystudio.engine.media.thumbs import frame_at
from toystudio.jobs.day_draft import clocks
from toystudio.storage.project_store import ProjectStore

THUMB = QSize(160, 90)
SKIPPED = 2  # dialog result: the user chose to go on without matching


class _Done(QObject):
    ready = Signal(int, str)


class _Thumb(QRunnable):
    """One frame from the middle of the clip's proxy, cached next to the other thumbnails."""

    def __init__(self, root: Path, m: MediaRow, done: _Done) -> None:
        super().__init__()
        self.root, self.m, self.done = root, m, done

    def run(self) -> None:
        out = self.root / "cache" / "thumbs" / f"c{self.m.id}.jpg"
        if not out.is_file():
            proxy = outputs_for(self.root / "cache", self.m.id).proxy
            with contextlib.suppress(RuntimeError, ValueError, OSError):
                at = (self.m.info.duration_us if self.m.info else 0) // 2
                frame_at(proxy, at, out)
        if out.is_file():
            self.done.ready.emit(self.m.id, str(out))


def _caption(m: MediaRow) -> str:
    secs = (m.info.duration_us if m.info else 0) // US_PER_S
    return f"{m.path.name} · {secs // 60}:{secs % 60:02d}"


class ClockDialog(QDialog):
    def __init__(self, root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.root = root
        self.setModal(True)
        self.resize(1100, 640)
        self._done = _Done(self)
        self._done.ready.connect(self._on_thumb)
        self._items: dict[int, QListWidgetItem] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        self.title = QLabel()
        self.title.setProperty("role", "title")
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setProperty("role", "body")
        self.hint.setProperty("tone", "muted")
        lay.addWidget(self.title)
        lay.addWidget(self.hint)
        lists = QHBoxLayout()
        self.cam_label, self.ref_label = QLabel(), QLabel()
        self.cam_list, self.ref_list = self._list(), self._list()
        for label, lst in ((self.cam_label, self.cam_list), (self.ref_label, self.ref_list)):
            col = QVBoxLayout()
            label.setProperty("role", "label")
            col.addWidget(label)
            col.addWidget(lst, 1)
            lists.addLayout(col, 1)
        lay.addLayout(lists, 1)
        buttons = QHBoxLayout()
        self.skip_button = QPushButton()
        set_variant(self.skip_button, "variant", "ghost")
        self.save_button = QPushButton()
        set_variant(self.save_button, "variant", "primary")
        self.save_button.setEnabled(False)
        buttons.addWidget(self.skip_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        lay.addLayout(buttons)
        self.skip_button.clicked.connect(lambda: self.done(SKIPPED))
        self.save_button.clicked.connect(self.save)
        self.cam_list.itemSelectionChanged.connect(self._update_save)
        self.ref_list.itemSelectionChanged.connect(self._update_save)
        self.camera = ""
        self._next()

    def _list(self) -> QListWidget:
        lst = QListWidget()
        lst.setViewMode(QListWidget.ViewMode.IconMode)
        lst.setIconSize(THUMB)
        lst.setResizeMode(QListWidget.ResizeMode.Adjust)
        lst.setMovement(QListWidget.Movement.Static)
        lst.setSpacing(6)
        lst.setWordWrap(True)
        return lst

    def _fill(self, lst: QListWidget, rows: list[MediaRow]) -> None:
        lst.clear()
        blank = QPixmap(THUMB)
        blank.fill(Qt.GlobalColor.black)
        for m in rows:
            item = QListWidgetItem(QIcon(blank), _caption(m))
            item.setData(Qt.ItemDataRole.UserRole, m.id)
            lst.addItem(item)
            self._items[m.id] = item
            QThreadPool.globalInstance().start(_Thumb(self.root, m, self._done))

    def _on_thumb(self, media_id: int, path: str) -> None:
        item = self._items.get(media_id)
        if item is not None:
            item.setIcon(QIcon(QPixmap(path)))

    def _next(self) -> None:
        """Show the next camera to match, or close when every camera is matched."""
        s = ProjectStore.open(self.root)
        try:
            c = clocks(s)
        finally:
            s.close()
        if not c.to_fix or c.reference is None:
            self.accept()
            return
        self.camera = c.to_fix[0]
        self._rows = {m.id: m for rows in c.clips.values() for m in rows}
        self._fill(self.cam_list, c.clips.get(self.camera, []))
        self._fill(self.ref_list, c.clips.get(c.reference, []))
        self._names = (Path(self.camera).name, Path(c.reference).name)
        self.retranslate()

    def _selected(self, lst: QListWidget) -> MediaRow | None:
        items = lst.selectedItems()
        return self._rows.get(items[0].data(Qt.ItemDataRole.UserRole)) if items else None

    def _update_save(self) -> None:
        self.save_button.setEnabled(
            bool(self._selected(self.cam_list) and self._selected(self.ref_list))
        )

    def save(self) -> None:
        cam, ref = self._selected(self.cam_list), self._selected(self.ref_list)
        t_cam = shot_time(cam.info.creation_time) if cam and cam.info else None
        t_ref = shot_time(ref.info.creation_time) if ref and ref.info else None
        if t_cam is None or t_ref is None:
            return
        s = ProjectStore.open(self.root)
        try:
            s.set_camera_clock(self.camera, offset_from_pair(t_cam, t_ref))
        finally:
            s.close()
        self._next()

    def retranslate(self) -> None:
        cam, ref = self._names if self.camera else ("", "")
        self.setWindowTitle(QCoreApplication.translate("ClockDialog", "Сверить часы камер"))
        self.title.setText(
            QCoreApplication.translate(
                "ClockDialog", "Часы камеры «%1» не совпадают с остальными"
            ).replace("%1", cam)
        )
        self.hint.setText(
            QCoreApplication.translate(
                "ClockDialog",
                "Выберите по одному клипу, снятому в одно время — например, кортеж или выход "
                "невесты. По этой паре программа поправит время всех клипов камеры.",
            )
        )
        self.cam_label.setText(
            QCoreApplication.translate("ClockDialog", "Камера «%1»").replace("%1", cam)
        )
        self.ref_label.setText(
            QCoreApplication.translate("ClockDialog", "Камера «%1» — по ней сверяем").replace(
                "%1", ref
            )
        )
        self.skip_button.setText(QCoreApplication.translate("ClockDialog", "Пропустить"))
        self.save_button.setText(QCoreApplication.translate("ClockDialog", "Сохранить"))
