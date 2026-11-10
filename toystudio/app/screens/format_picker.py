"""Screen «Что монтируем?»: the edit format decides the tracks the files go to."""

from __future__ import annotations

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QEvent, Signal
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
_COLUMNS = 3


class FormatPickerScreen(QWidget):
    chosen = Signal(object)  # ProjectFormat

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)
        self._title = QLabel()
        self._title.setProperty("role", "title")
        root.addWidget(self._title)
        grid = QGridLayout()
        grid.setSpacing(16)
        self._texts: list[tuple[QLabel, QLabel, list[QPushButton]]] = []
        self.buttons: dict[ProjectFormat, QPushButton] = {}
        for i, (_title, _desc, choices) in enumerate(_CARDS):
            card = QFrame()
            card.setObjectName("FormatCard")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(20, 20, 20, 20)
            lay.setSpacing(8)
            title = QLabel()
            title.setProperty("role", "heading")
            desc = QLabel()
            desc.setWordWrap(True)
            desc.setProperty("role", "caption")
            desc.setProperty("tone", "muted")
            lay.addWidget(title)
            lay.addWidget(desc, 1)
            row = QHBoxLayout()
            row.setSpacing(8)
            buttons = []
            for fmt, _label in choices:
                b = QPushButton()
                b.setMinimumHeight(36)
                set_variant(b, "variant", "primary")
                b.clicked.connect(lambda _=False, f=fmt: self.chosen.emit(f))
                row.addWidget(b)
                buttons.append(b)
                self.buttons[fmt] = b
            lay.addLayout(row)
            grid.addWidget(card, i // _COLUMNS, i % _COLUMNS)
            self._texts.append((title, desc, buttons))
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
