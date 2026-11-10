"""Screen 1: grid of project cards, search, status filter, drop target for a folder."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QEvent, QPoint, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from toystudio.app.theme.apply import set_variant
from toystudio.app.viewmodels.projects_vm import ProjectItem, ProjectsVM
from toystudio.app.widgets.drop_zone import VIDEO_EXT
from toystudio.app.widgets.empty_state import EmptyState
from toystudio.core.format import format_eta
from toystudio.core.models import ProjectFormat, ToiKind

_KIND_TEXT: dict[ToiKind, str] = {
    ToiKind.WEDDING: QT_TRANSLATE_NOOP("ToiKind", "Свадьба"),
    ToiKind.TOI: QT_TRANSLATE_NOOP("ToiKind", "Той"),
    ToiKind.JUBILEE: QT_TRANSLATE_NOOP("ToiKind", "Юбилей"),
    ToiKind.BESH_BARMAK: QT_TRANSLATE_NOOP("ToiKind", "Беш-бармак"),
    ToiKind.SUNNOT: QT_TRANSLATE_NOOP("ToiKind", "Сүннөт той"),
    ToiKind.KYZ_UZATUU: QT_TRANSLATE_NOOP("ToiKind", "Кыз узатуу"),
    ToiKind.OTHER: QT_TRANSLATE_NOOP("ToiKind", "Другое"),
}
# format labels share the «NewProjectScreen» context of their translations
_FORMAT_TEXT: dict[ProjectFormat, str] = {
    ProjectFormat.WEDDING_EXPRESS: QT_TRANSLATE_NOOP(
        "NewProjectScreen", "Свадьба · экспресс-видео"
    ),
    ProjectFormat.WEDDING_FULL: QT_TRANSLATE_NOOP("NewProjectScreen", "Свадьба · полный фильм"),
    ProjectFormat.LOVE_STORY: QT_TRANSLATE_NOOP("NewProjectScreen", "Love Story"),
    ProjectFormat.PODCAST: QT_TRANSLATE_NOOP("NewProjectScreen", "Подкаст или интервью"),
    ProjectFormat.AD: QT_TRANSLATE_NOOP("NewProjectScreen", "Рекламный ролик"),
    ProjectFormat.TOI: QT_TRANSLATE_NOOP("NewProjectScreen", "Той, юбилей и другое"),
}
_FILTERS: tuple[tuple[str | None, str], ...] = (
    (None, QT_TRANSLATE_NOOP("ProjectsScreen", "Все проекты")),
    ("analysis", QT_TRANSLATE_NOOP("ProjectsScreen", "Идёт анализ")),
    ("analysis_done", QT_TRANSLATE_NOOP("ProjectsScreen", "Анализ завершён")),
    ("files", QT_TRANSLATE_NOOP("ProjectsScreen", "Файлы")),
    ("error", QT_TRANSLATE_NOOP("ProjectsScreen", "Ошибка")),
)


def columns_for(width: int) -> int:
    return 4 if width >= 1800 else 3 if width >= 1350 else 2


def _has_media(root: Path) -> bool:
    try:
        return (root / "media").is_dir() and any((root / "media").iterdir())
    except OSError:
        return True  # unknown: show the stronger warning


def video_files(paths: list[Path]) -> list[Path]:
    """Videos among dropped files; folders are searched recursively."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in VIDEO_EXT))
        elif p.suffix.lower() in VIDEO_EXT:
            out.append(p)
    return out


class ProjectsScreen(QWidget):
    def __init__(self, vm: ProjectsVM, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.vm = vm
        self.setAcceptDrops(True)
        self._cards: list = []
        self._pixmaps: dict[tuple[Path, float], QPixmap] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)
        head = QHBoxLayout()
        head.setSpacing(12)
        self._title = QLabel()
        self._title.setProperty("role", "title")
        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(260)
        self.filter_box = QComboBox()
        self.new_button = QPushButton()
        set_variant(self.new_button, "variant", "primary")
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(self.search_edit)
        head.addWidget(self.filter_box)
        head.addWidget(self.new_button)
        root.addLayout(head)

        self._stack = QStackedWidget()
        self.empty_state = EmptyState("film", "", "", "")
        self._stack.addWidget(self.empty_state)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(16)
        pl.addWidget(self._grid_host)
        self._no_match = QLabel()
        self._no_match.setProperty("tone", "muted")
        pl.addWidget(self._no_match)
        pl.addStretch(1)
        self._scroll.setWidget(page)
        self._stack.addWidget(self._scroll)
        root.addWidget(self._stack, 1)

        for status, _ in _FILTERS:
            self.filter_box.addItem("", status)
        self.new_button.clicked.connect(lambda: self.vm.request_new([]))
        self.empty_state.activated.connect(lambda: self.vm.request_new([]))
        self.search_edit.textChanged.connect(self.vm.search)
        self.filter_box.currentIndexChanged.connect(
            lambda i: self.vm.filter_status(self.filter_box.itemData(i))
        )
        self.vm.changed.connect(self._refresh)
        self.retranslate()
        self._refresh()

    # ---- queries (tests)
    def columns(self) -> int:
        return columns_for(self.width())

    def card_roots(self) -> list[Path]:
        return [i.root for i in self.vm.items] if self._cards else []

    def card_at(self, index: int):
        return self._cards[index]

    # ---- rendering
    def _kind_text(self, kind: ToiKind) -> str:
        return QCoreApplication.translate("ToiKind", _KIND_TEXT[kind])

    def _preview(self, path: Path | None) -> QPixmap | None:
        if path is None:
            return None
        try:
            key = (path, path.stat().st_mtime)
        except OSError:
            return None
        if key not in self._pixmaps:
            self._pixmaps[key] = QPixmap(str(path))
        return self._pixmaps[key]

    def _fill(self, card, item: ProjectItem) -> None:
        lang = "ky" if QCoreApplication.translate("ProjectsScreen", "ru") == "ky" else "ru"
        eta = None if item.eta_s is None else format_eta(item.eta_s, lang)
        card.set_data(
            item.name,
            self._kind_text(item.kind)
            if item.format is ProjectFormat.TOI
            else QCoreApplication.translate("NewProjectScreen", _FORMAT_TEXT[item.format]),
            item.event_date.strftime("%d.%m.%Y") if item.event_date else "",
            item.duration_us or None,
            item.status,
            item.percent,
            eta,
            self._preview(item.preview_path),
        )

    def _refresh(self) -> None:
        from toystudio.app.widgets.project_card import ProjectCard

        items = self.vm.items
        known = [c.property("root") for c in self._cards]
        if known == [str(i.root) for i in items] and items:
            for card, item in zip(self._cards, items, strict=True):
                self._fill(card, item)
        else:
            for c in self._cards:
                self._grid.removeWidget(c)
                c.deleteLater()
            self._cards = []
            for item in items:
                card = ProjectCard()
                card.setProperty("root", str(item.root))
                card.opened.connect(lambda r=item.root: self.vm.open(r))
                card.context_requested.connect(lambda pos, r=item.root: self._menu_at(r, pos))
                self._fill(card, item)
                self._cards.append(card)
            self._relayout()
        no_projects = self.vm.total == 0
        self._stack.setCurrentWidget(self.empty_state if no_projects else self._scroll)
        self._no_match.setVisible(not no_projects and not items)

    def _relayout(self) -> None:
        cols = self.columns()
        for i in range(self._grid.columnCount()):
            self._grid.setColumnStretch(i, 0)
        for n, card in enumerate(self._cards):
            self._grid.addWidget(card, n // cols, n % cols, Qt.AlignmentFlag.AlignTop)
        for c in range(cols):
            self._grid.setColumnStretch(c, 1)

    def resizeEvent(self, e: QResizeEvent) -> None:
        super().resizeEvent(e)
        self._relayout()

    # ---- context menu and actions
    def build_menu(self, root: Path) -> QMenu:
        menu = QMenu(self)
        busy = self.vm.is_busy(root.resolve())
        rename = menu.addAction(QCoreApplication.translate("ProjectsScreen", "Переименовать"))
        rename.setEnabled(not busy)
        rename.triggered.connect(lambda: self.rename_project(root))
        menu.addAction(
            QCoreApplication.translate("ProjectsScreen", "Открыть папку")
        ).triggered.connect(lambda: self.open_folder(root))
        menu.addAction(
            QCoreApplication.translate("ProjectsScreen", "Удалить проект (видео не удаляется)")
        ).triggered.connect(lambda: self.delete_project(root))
        return menu

    def _menu_at(self, root: Path, pos: QPoint) -> None:
        self.build_menu(root).exec(pos)

    def open_folder(self, root: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def rename_project(self, root: Path) -> None:
        item = self.vm.item(root)
        if item is None or self.vm.is_busy(root.resolve()):
            return
        name, ok = QInputDialog.getText(
            self,
            QCoreApplication.translate("ProjectsScreen", "Переименовать"),
            QCoreApplication.translate("ProjectsScreen", "Название"),
            QLineEdit.EchoMode.Normal,
            item.name,
        )
        if ok:
            self.vm.rename(root, name)

    def delete_project(self, root: Path) -> None:
        item = self.vm.item(root)
        if self.vm.is_busy(root.resolve()):
            QMessageBox.information(
                self,
                QCoreApplication.translate("ProjectsScreen", "Удалить проект"),
                QCoreApplication.translate(
                    "ProjectsScreen", "Идёт анализ. Остановите его, чтобы удалить проект."
                ),
            )
            return
        text = QCoreApplication.translate(
            "ProjectsScreen", "Проект «%1» будет удалён вместе с результатами анализа."
        ).replace("%1", item.name if item else root.name)
        has_media = _has_media(root)
        if (item is not None and item.has_copies) or has_media:
            text += " " + QCoreApplication.translate(
                "ProjectsScreen", "Копии видео в папке проекта тоже удалятся."
            )
        else:
            text += " " + QCoreApplication.translate(
                "ProjectsScreen", "Исходные видео не удаляются."
            )
        answer = QMessageBox.question(
            self,
            QCoreApplication.translate("ProjectsScreen", "Удалить проект"),
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if (
            answer == QMessageBox.StandardButton.Yes
            and not self.vm.delete(root)
            and self.vm.last_error is not None
        ):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(QCoreApplication.translate("ProjectsScreen", "Удалить проект"))
            box.setText(
                QCoreApplication.translate(
                    "ProjectsScreen",
                    "Не удалось удалить проект — закройте программы, "
                    "которые используют его файлы, и попробуйте снова.",
                )
            )
            box.setDetailedText(self.vm.last_error)
            box.exec()

    # ---- drag and drop: the whole window is the drop zone
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        files = video_files(paths)
        if files:
            e.acceptProposedAction()
            self.vm.request_new(files)

    # ---- i18n
    def retranslate(self) -> None:
        self._title.setText(QCoreApplication.translate("ProjectsScreen", "Проекты"))
        self.search_edit.setPlaceholderText(
            QCoreApplication.translate("ProjectsScreen", "Поиск по названию")
        )
        self.new_button.setText(QCoreApplication.translate("ProjectsScreen", "Новый проект"))
        for i, (_, label) in enumerate(_FILTERS):
            self.filter_box.setItemText(i, QCoreApplication.translate("ProjectsScreen", label))
        self.empty_state.set_texts(
            QCoreApplication.translate("ProjectsScreen", "Смонтируем первый той"),
            QCoreApplication.translate(
                "ProjectsScreen", "Перетащите сюда папку с видео или создайте проект"
            ),
            QCoreApplication.translate("ProjectsScreen", "Новый проект"),
        )
        self._no_match.setText(QCoreApplication.translate("ProjectsScreen", "Ничего не найдено"))
        if hasattr(self, "_cards"):
            self._refresh()

    def changeEvent(self, e: QEvent) -> None:
        if e.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(e)
