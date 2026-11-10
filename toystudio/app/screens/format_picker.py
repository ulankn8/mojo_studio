"""Screen «Что монтируем?»: the edit format decides the tracks the files go to."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QEvent, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from toystudio.app.theme.apply import set_variant
from toystudio.app.widgets.icons import IconLabel
from toystudio.core.models import ProjectFormat

_T = "FormatPicker"
# card: title, description, buttons (format, label); one button per card except the wedding
_CARDS: tuple[tuple[str, str, tuple[tuple[ProjectFormat, str], ...]], ...] = (
    (
        QT_TRANSLATE_NOOP("FormatPicker", "Свадьба"),
        QT_TRANSLATE_NOOP(
            "FormatPicker",
            "Дом жениха, дорога, невеста, лав-стори, фуршет и ресторан с двух камер",
        ),
        (
            (ProjectFormat.WEDDING_EXPRESS, QT_TRANSLATE_NOOP("FormatPicker", "Экспресс-видео")),
            (ProjectFormat.WEDDING_FULL, QT_TRANSLATE_NOOP("FormatPicker", "Полный фильм")),
        ),
    ),
    (
        QT_TRANSLATE_NOOP("FormatPicker", "Love Story"),
        QT_TRANSLATE_NOOP("FormatPicker", "Съёмка молодожёнов на локации, клип под музыку"),
        ((ProjectFormat.LOVE_STORY, QT_TRANSLATE_NOOP("FormatPicker", "Выбрать")),),
    ),
    (
        QT_TRANSLATE_NOOP("FormatPicker", "Подкаст или интервью"),
        QT_TRANSLATE_NOOP("FormatPicker", "Основная камера, вторая камера и звук с микрофонов"),
        ((ProjectFormat.PODCAST, QT_TRANSLATE_NOOP("FormatPicker", "Выбрать")),),
    ),
    (
        QT_TRANSLATE_NOOP("FormatPicker", "Рекламный ролик"),
        QT_TRANSLATE_NOOP("FormatPicker", "Ролик до 10–20 минут из съёмки и музыки"),
        ((ProjectFormat.AD, QT_TRANSLATE_NOOP("FormatPicker", "Выбрать")),),
    ),
    (
        QT_TRANSLATE_NOOP("FormatPicker", "Той, юбилей и другое"),
        QT_TRANSLATE_NOOP("FormatPicker", "Видео события и музыка, как раньше"),
        ((ProjectFormat.TOI, QT_TRANSLATE_NOOP("FormatPicker", "Выбрать")),),
    ),
)
_ICONS = ("gem", "heart", "mic", "megaphone", "party")
# (row, column, column span): the wedding, the studio's main work, leads on a double card
_PLACES = ((0, 0, 2), (0, 2, 1), (1, 0, 1), (1, 1, 1), (1, 2, 1))


class _Card(QFrame):
    """A format card; a card with one choice is clickable as a whole."""

    def __init__(self, on_click: Callable[[], None] | None) -> None:
        super().__init__()
        self.setObjectName("FormatCard")
        self._on_click = on_click
        if on_click is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if self._on_click is not None and e.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mouseReleaseEvent(e)


class FormatPickerScreen(QWidget):
    chosen = Signal(object)  # ProjectFormat

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(20)
        self._title = QLabel()
        self._title.setProperty("role", "title")
        root.addWidget(self._title)
        grid = QGridLayout()
        grid.setSpacing(16)
        self._texts: list[tuple[QLabel, QLabel, list[QPushButton]]] = []
        self.buttons: dict[ProjectFormat, QPushButton] = {}
        for (_title, _desc, choices), name, (row, col, span) in zip(
            _CARDS, _ICONS, _PLACES, strict=True
        ):
            single = choices[0][0] if len(choices) == 1 else None
            card = _Card(None if single is None else lambda f=single: self.chosen.emit(f))
            lay = QVBoxLayout(card)
            lay.setContentsMargins(24, 22, 24, 22)
            lay.setSpacing(6)
            mark = IconLabel(28)
            mark.set_icon(name, "garnet-text")
            title = QLabel()
            title.setProperty("role", "heading")
            desc = QLabel()
            desc.setWordWrap(True)
            desc.setProperty("role", "caption")
            desc.setProperty("tone", "muted")
            lay.addWidget(mark)
            lay.addSpacing(6)
            lay.addWidget(title)
            lay.addWidget(desc, 1)
            lay.addSpacing(10)
            buttons_row = QHBoxLayout()
            buttons_row.setSpacing(8)
            buttons = []
            for k, (fmt, _label) in enumerate(choices):
                b = QPushButton()
                b.setMinimumHeight(36)
                if len(choices) > 1 and k == len(choices) - 1:  # the full film: the main choice
                    set_variant(b, "variant", "primary")
                b.clicked.connect(lambda _=False, f=fmt: self.chosen.emit(f))
                buttons_row.addWidget(b)
                buttons.append(b)
                self.buttons[fmt] = b
            if len(choices) == 1:
                buttons_row.addStretch(1)
            lay.addLayout(buttons_row)
            grid.addWidget(card, row, col, 1, span)
            self._texts.append((title, desc, buttons))
        for col in range(3):
            grid.setColumnStretch(col, 1)
        root.addLayout(grid)
        root.addStretch(1)
        self.retranslate()

    def retranslate(self) -> None:
        self._title.setText(QCoreApplication.translate("FormatPicker", "Что монтируем?"))
        for (title, desc, choices), (title_w, desc_w, buttons) in zip(
            _CARDS, self._texts, strict=True
        ):
            title_w.setText(QCoreApplication.translate(_T, title))
            desc_w.setText(QCoreApplication.translate(_T, desc))
            for (_fmt, label), b in zip(choices, buttons, strict=True):
                b.setText(QCoreApplication.translate(_T, label))

    def changeEvent(self, e: QEvent) -> None:
        if e.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(e)
