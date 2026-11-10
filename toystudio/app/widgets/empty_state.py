"""Empty state: large icon, one inviting phrase, a short explanation, one action."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from toystudio.app.theme.apply import set_variant
from toystudio.app.widgets.icons import IconLabel


class EmptyState(QWidget):
    activated = Signal()

    def __init__(
        self,
        icon: str,
        title: str,
        body: str,
        action: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon = IconLabel(48)
        self._icon.set_icon(icon, "ink-faint")
        self._title = QLabel(title)
        self._title.setProperty("role", "display")
        self._title.setWordWrap(True)
        self._title.setMinimumWidth(560)  # the headline stays on one line in Russian and Kyrgyz
        self._body = QLabel(body)
        self._body.setProperty("role", "body")
        self._body.setProperty("tone", "muted")
        self._body.setWordWrap(True)
        self.button = QPushButton(action or "")
        set_variant(self.button, "variant", "primary")
        self.button.setVisible(action is not None)
        self.button.clicked.connect(self.activated)
        for w in (self._icon, self._title, self._body):
            lay.addWidget(w, 0, Qt.AlignmentFlag.AlignHCenter)
        for lbl in (self._title, self._body):
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_texts(self, title: str, body: str, action: str | None) -> None:
        self._title.setText(title)
        self._body.setText(body)
        self.button.setText(action or "")
        self.button.setVisible(action is not None)
