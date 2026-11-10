"""Drop zone of one track; turns into a file list with progress once files are added."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QEvent, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from toystudio.app.theme.apply import set_variant
from toystudio.app.widgets.elided import ElidedLabel
from toystudio.app.widgets.icons import IconLabel
from toystudio.core.format import format_size, format_timecode
from toystudio.core.models import MediaKind, MediaRole

VIDEO_EXT = {".mp4", ".mov", ".mts", ".m2ts", ".mkv", ".avi"}
MUSIC_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

RowState = Literal["copying", "checking", "ready", "corrupt", "missing", "duplicate"]

# state -> (label tone, source text); "copying" gets the percent appended in the label.
_STATE_TEXT: dict[str, str] = {
    "copying": QT_TRANSLATE_NOOP("DropZone", "Копируем…"),
    "checking": QT_TRANSLATE_NOOP("DropZone", "Проверяем…"),
    "ready": QT_TRANSLATE_NOOP("DropZone", "Готово ✓"),
    "corrupt": QT_TRANSLATE_NOOP("DropZone", "Файл повреждён !"),
    "missing": QT_TRANSLATE_NOOP("DropZone", "Файл не найден"),
    "duplicate": QT_TRANSLATE_NOOP("DropZone", "Уже добавлен"),
}
_STATE_TONE = {
    "copying": "teal",
    "checking": "teal",
    "ready": "done",
    "corrupt": "error",
    "missing": "error",
    "duplicate": "muted",
}
_PLURAL = (
    QT_TRANSLATE_NOOP("DropZone", "%1 файл"),
    QT_TRANSLATE_NOOP("DropZone", "%1 файла"),
    QT_TRANSLATE_NOOP("DropZone", "%1 файлов"),
)


@dataclass(frozen=True)
class FileRow:
    media_id: str
    name: str
    size: int
    duration_us: int | None
    state: RowState
    percent: int | None


def collect_media_paths(paths: list[Path], kind: MediaKind) -> list[Path]:
    """Files with a matching extension; folders are walked recursively."""
    exts = VIDEO_EXT if kind is MediaKind.VIDEO else MUSIC_EXT
    found: list[Path] = []
    for p in paths:
        if p.is_dir():
            found.extend(
                sorted(c for c in p.rglob("*") if c.is_file() and c.suffix.lower() in exts)
            )
        elif p.suffix.lower() in exts:
            found.append(p)
    return found


def _plural_index(n: int) -> int:
    if 11 <= n % 100 <= 14:
        return 2
    if n % 10 == 1:
        return 0
    if 2 <= n % 10 <= 4:
        return 1
    return 2


class DropZone(QFrame):
    files_dropped = Signal(list)
    replace_requested = Signal(str)

    def __init__(
        self, role: MediaRole | MediaKind, parent: QWidget | None = None, *, compact: bool = False
    ) -> None:
        super().__init__(parent)
        self.role = role if isinstance(role, MediaRole) else MediaRole.default_for(role)
        self._kind = self.role.kind
        self._rows: list[FileRow] = []
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        root = QVBoxLayout(self)
        # compact: one row (icon, prompt, buttons) for a screen with several tracks
        root.setContentsMargins(*((16, 10, 12, 10) if compact else (16, 16, 16, 16)))
        root.setSpacing(8)
        # empty view
        self._empty = QWidget()
        elay = QVBoxLayout(self._empty)
        elay.setSpacing(12)
        self._icon = IconLabel(24)
        self._icon.set_icon("upload", "ink-faint")
        self._prompt = QLabel()
        self._prompt.setProperty("role", "prompt")
        self.choose_button = QPushButton()
        self.choose_button.clicked.connect(self._browse)
        self.folder_button = QPushButton()  # a music library is a folder
        self.folder_button.clicked.connect(self._browse_folder)
        self.folder_button.setVisible(self.role is MediaRole.MUSIC)
        buttons = QHBoxLayout()
        buttons.addWidget(self.choose_button)
        buttons.addWidget(self.folder_button)
        if compact:
            elay.setDirection(QVBoxLayout.Direction.LeftToRight)
            elay.setContentsMargins(0, 0, 0, 0)
            elay.addWidget(self._icon)
            elay.addWidget(self._prompt, 1)
            elay.addLayout(buttons)
        else:
            for w in (self._icon, self._prompt):
                elay.addWidget(w, 0, Qt.AlignmentFlag.AlignHCenter)
            elay.addLayout(buttons)
            buttons.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(self._empty)
        # list view
        self._list = QWidget()
        llay = QVBoxLayout(self._list)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.setSpacing(0)
        self._summary = QLabel()
        self._summary.setProperty("role", "label")
        self._summary.setProperty("tone", "muted")
        llay.addWidget(self._summary)
        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(0)
        llay.addLayout(self._rows_box)
        self.add_button = QPushButton()
        self.add_button.clicked.connect(self._browse)
        llay.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self._list)
        self._list.hide()
        self._row_widgets: list[tuple[QLabel, QPushButton]] = []
        self.retranslate()

    # ---- drag and drop
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            set_variant(self, "dragover", "true")
        else:
            e.ignore()

    def dragLeaveEvent(self, e: QDragLeaveEvent) -> None:
        set_variant(self, "dragover", "false")
        super().dragLeaveEvent(e)

    def dropEvent(self, e: QDropEvent) -> None:
        set_variant(self, "dragover", "false")
        urls = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        e.acceptProposedAction()
        files = collect_media_paths(urls, self._kind)
        if files:
            self.files_dropped.emit(files)

    def _browse(self) -> None:
        exts = VIDEO_EXT if self._kind is MediaKind.VIDEO else MUSIC_EXT
        pattern = " ".join(f"*{x}" for x in sorted(exts))
        names, _ = QFileDialog.getOpenFileNames(self, "", "", pattern)
        if names:
            self.files_dropped.emit([Path(n) for n in names])

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "")
        if folder:
            files = collect_media_paths([Path(folder)], self._kind)
            if files:
                self.files_dropped.emit(files)

    # ---- rows
    def set_rows(self, rows: list[FileRow]) -> None:
        self._rows = list(rows)
        while self._rows_box.count():
            item = self._rows_box.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        self._row_widgets = []
        for r in self._rows:
            self._rows_box.addWidget(self._build_row(r))
        self._empty.setVisible(not rows)
        self._list.setVisible(bool(rows))
        self.retranslate()

    def _build_row(self, r: FileRow) -> QFrame:
        row = QFrame()
        row.setObjectName("DropRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(12)
        name = ElidedLabel(r.name)
        lay.addWidget(name, 1)
        if r.duration_us is not None:
            tc = QLabel(format_timecode(r.duration_us))
            tc.setProperty("role", "timecode")
            tc.setProperty("tone", "muted")
            lay.addWidget(tc)
        size = QLabel(format_size(r.size))
        size.setProperty("role", "caption")
        size.setProperty("tone", "muted")
        lay.addWidget(size)
        status = QLabel()
        status.setProperty("role", "label")
        status.setProperty("tone", _STATE_TONE[r.state])
        lay.addWidget(status)
        replace = QPushButton(row)
        replace.clicked.connect(lambda _=False, mid=r.media_id: self.replace_requested.emit(mid))
        lay.addWidget(replace)
        replace.setVisible(r.state == "corrupt")
        self._row_widgets.append((status, replace))
        return row

    def _status_text(self, r: FileRow) -> str:
        text = QCoreApplication.translate("DropZone", _STATE_TEXT[r.state])
        if r.state == "copying" and r.percent is not None:
            text = f"{text} {r.percent}%"
        return text

    def row_status_texts(self) -> list[str]:
        return [s.text() for s, _ in self._row_widgets]

    def replace_buttons(self) -> list[QPushButton]:
        return [b for _, b in self._row_widgets if not b.isHidden()]

    def summary_text(self) -> str:
        return self._summary.text() if self._rows else ""

    def drop_text(self) -> str:
        return self._prompt.text()

    def _summary_text(self) -> str:
        n = len(self._rows)
        count = QCoreApplication.translate("DropZone", _PLURAL[_plural_index(n)]).replace(
            "%1", str(n)
        )
        parts = [count, format_size(sum(r.size for r in self._rows))]
        durations = [r.duration_us for r in self._rows if r.duration_us is not None]
        if durations:
            parts.append(format_timecode(sum(durations)))
        return " · ".join(parts)

    def retranslate(self) -> None:
        if self._kind is MediaKind.VIDEO:
            prompt = QCoreApplication.translate("DropZone", "Перетащите видео сюда")
        elif self.role is MediaRole.SPEECH:
            prompt = QCoreApplication.translate("DropZone", "Перетащите звук сюда")
        else:
            prompt = QCoreApplication.translate("DropZone", "Перетащите музыку сюда")
        self._prompt.setText(prompt)
        self.choose_button.setText(QCoreApplication.translate("DropZone", "Выбрать файлы"))
        self.folder_button.setText(QCoreApplication.translate("DropZone", "Выбрать папку"))
        self.add_button.setText(QCoreApplication.translate("DropZone", "Добавить ещё"))
        replace = QCoreApplication.translate("DropZone", "Заменить")
        for r, (status, btn) in zip(self._rows, self._row_widgets, strict=True):
            status.setText(self._status_text(r))
            btn.setText(replace)
        if self._rows:
            self._summary.setText(self._summary_text())

    def changeEvent(self, e: QEvent) -> None:
        if e.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(e)
