"""48 px top bar: logo, back to projects, project stepper, RU/KY switch, theme switch."""

from __future__ import annotations

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QEvent, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QWidget

from toystudio.app.theme.apply import set_variant, token
from toystudio.app.widgets.elided import ElidedButton
from toystudio.app.widgets.icons import app_icon, icon

_STEPS = (
    QT_TRANSLATE_NOOP("Shell", "Файлы"),
    QT_TRANSLATE_NOOP("Shell", "Анализ"),
    QT_TRANSLATE_NOOP("Shell", "Задание"),
    QT_TRANSLATE_NOOP("Shell", "Редактор"),
    QT_TRANSLATE_NOOP("Shell", "Экспорт"),
)
_LANG_LABELS = {"ru": "Русский", "ky": "Кыргызча"}


class TopBar(QWidget):
    language_changed = Signal(str)
    theme_changed = Signal(str)
    projects_requested = Signal()
    step_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setFixedHeight(48)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._theme = "dark"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(8)
        mark = QLabel()
        mark.setPixmap(app_icon(64).pixmap(22, 22))
        self._logo = QLabel("T-Studio")
        self._logo.setObjectName("Logo")
        lay.addWidget(mark)
        lay.addSpacing(2)
        lay.addWidget(self._logo)
        lay.addSpacing(16)
        self._projects_btn = QPushButton(QCoreApplication.translate("Shell", "Проекты"))
        set_variant(self._projects_btn, "variant", "ghost")
        self._projects_btn.clicked.connect(self.projects_requested)
        lay.addWidget(self._projects_btn)
        self._steps: list[ElidedButton] = []
        self._step_group = QButtonGroup(self)
        self._step_group.setExclusive(True)
        for i, text in enumerate(_STEPS):
            b = ElidedButton(QCoreApplication.translate("Shell", text))
            b.setCheckable(True)
            b.setEnabled(i == 0)
            set_variant(b, "variant", "ghost")
            b.clicked.connect(lambda _=False, n=i: self._on_step(n))
            self._step_group.addButton(b)
            lay.addWidget(b)
            self._steps.append(b)
        self._current = 0
        self._steps[0].setChecked(True)
        lay.addStretch(1)
        self._lang: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for code, label in _LANG_LABELS.items():
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty("chip", "true")
            b.clicked.connect(lambda _=False, c=code: self.language_changed.emit(c))
            group.addButton(b)
            lay.addWidget(b)
            self._lang[code] = b
        self._lang["ru"].setChecked(True)
        self._theme_btn = QPushButton(QCoreApplication.translate("Shell", "Сменить тему"))
        set_variant(self._theme_btn, "variant", "ghost")
        self._theme_btn.clicked.connect(self._toggle_theme)
        lay.addWidget(self._theme_btn)
        self._theme_icon()

    def _toggle_theme(self) -> None:
        self._theme = "light" if self._theme == "dark" else "dark"
        self.theme_changed.emit(self._theme)
        self._theme_icon()

    def _theme_icon(self) -> None:
        """The icon shows the theme a click switches to."""
        self._theme_btn.setIcon(icon("sun" if self._theme == "dark" else "moon",
                                     token("ink-muted"), 16))  # fmt: skip

    def set_step(self, i: int, reachable: int) -> None:
        self._current = i
        for n, b in enumerate(self._steps):
            b.setChecked(n == i)
            b.setEnabled(n <= reachable)

    def _on_step(self, i: int) -> None:
        if i != self._current:
            self.step_clicked.emit(i)

    def set_step_enabled(self, i: int, on: bool) -> None:
        self._steps[i].setEnabled(on)

    def set_project_mode(self, on: bool) -> None:
        """Stepper and «Проекты» are shown only while a project is open."""
        self._projects_btn.setVisible(on)
        for b in self._steps:
            b.setVisible(on)

    def click_projects(self) -> None:
        self._projects_btn.click()

    def current_step(self) -> int:
        return self._current

    def step_labels(self) -> list[str]:
        return [b.full_text() for b in self._steps]

    def step_enabled(self) -> list[bool]:
        return [b.isEnabled() for b in self._steps]

    def language_labels(self) -> tuple[str, str]:
        return (self._lang["ru"].text(), self._lang["ky"].text())

    def set_language_checked(self, code: str) -> None:
        self._lang[code].setChecked(True)

    def click_language(self, code: str) -> None:
        self._lang[code].click()

    def click_theme(self) -> None:
        self._theme_btn.click()

    def retranslate(self) -> None:
        for b, text in zip(self._steps, _STEPS, strict=True):
            b.setText(QCoreApplication.translate("Shell", text))
        self._projects_btn.setText(QCoreApplication.translate("Shell", "Проекты"))
        self._theme_btn.setText(QCoreApplication.translate("Shell", "Сменить тему"))

    def changeEvent(self, e: QEvent) -> None:
        if e.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(e)
