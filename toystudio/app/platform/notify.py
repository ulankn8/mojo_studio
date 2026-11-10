"""Windows notification through the tray icon, and minimize-to-tray."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from toystudio.app.widgets.icons import app_icon


class Notifier:
    def __init__(self, icon: QIcon) -> None:
        # a null icon leaves an invisible tray slot on Windows: the window could not come back
        self.icon = icon if not icon.isNull() else app_icon()
        self._tray: QSystemTrayIcon | None = None
        self._on_click: Callable[[], None] | None = None
        self._window: QWidget | None = None
        self.menu = QMenu()
        self.open_action = QAction(self.menu)
        self.quit_action = QAction(self.menu)
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.quit_action)
        self.open_action.triggered.connect(self.restore)
        self.quit_action.triggered.connect(self._quit)
        self.menu.aboutToShow.connect(self.retranslate)
        self.retranslate()

    def retranslate(self) -> None:
        self.open_action.setText(QCoreApplication.translate("Notifier", "Открыть"))
        self.quit_action.setText(QCoreApplication.translate("Notifier", "Выйти"))

    def _ensure_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        if self._tray is None:
            tray = QSystemTrayIcon(self.icon)
            tray.setContextMenu(self.menu)
            tray.setToolTip("T-Studio")
            tray.messageClicked.connect(self._clicked)
            tray.activated.connect(self._tray_activated)
            self._tray = tray
        self._tray.show()
        return self._tray

    def notify(self, title: str, body: str, on_click: Callable[[], None]) -> None:
        tray = self._ensure_tray()
        if tray is None:
            return
        self._on_click = on_click
        tray.showMessage(title, body, self.icon)

    def minimize_to_tray(self, window: QWidget) -> None:
        tray = self._ensure_tray()
        if tray is None:
            window.showMinimized()
            return
        self._window = window
        window.hide()

    def restore(self) -> None:
        """Bring the window hidden in the tray back."""
        if self._window is None:
            return
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()

    def _quit(self) -> None:
        # closing the window runs its usual close checks (a running analysis asks first)
        if self._window is not None:
            self.restore()
            self._window.close()
        else:
            QApplication.quit()

    def _clicked(self) -> None:
        if self._on_click is not None:
            self._on_click()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore()
