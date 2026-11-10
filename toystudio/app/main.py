"""ToyStudio entry point: single instance, dark theme by default."""

from __future__ import annotations

import getpass
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from toystudio.app.shell import Shell
from toystudio.app.theme.apply import apply_theme, load_fonts
from toystudio.app.widgets.icons import app_icon
from toystudio.storage.app_store import AppStore

log = logging.getLogger(__name__)
PROJECTS_FOLDER = "T-Studio"
HOME_ENV = "TOYSTUDIO_HOME"


def app_paths(
    env: Mapping[str, str] | None = None,
    platform: str = sys.platform,
    home: Path | None = None,
) -> tuple[Path, Path]:
    """(default projects folder, app.db). TOYSTUDIO_HOME puts both under one folder."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    override = env.get(HOME_ENV)
    if override:
        base = Path(override)
        return base / "projects", base / "app.db"
    if platform == "win32":
        profile = Path(env.get("USERPROFILE") or home)
        appdata = Path(env.get("APPDATA") or profile / "AppData" / "Roaming")
        return profile / "Videos" / PROJECTS_FOLDER, appdata / "ToyStudio" / "app.db"
    videos = "Movies" if platform == "darwin" else "Videos"
    return home / videos / PROJECTS_FOLDER, home / ".toystudio" / "app.db"


def _server_name() -> str:
    return f"ToyStudio-{getpass.getuser()}"


def _signal_running_instance(name: str) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"raise")
    sock.flush()
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    name = _server_name()
    if _signal_running_instance(name):
        return 0
    QLocalServer.removeServer(name)  # stale socket from a crashed run
    server = QLocalServer()
    server.listen(name)
    app.setWindowIcon(app_icon())  # also the tray icon (Notifier) and the taskbar button
    load_fonts()
    apply_theme(app, "dark")
    projects_dir, app_db = app_paths()
    projects_dir.mkdir(parents=True, exist_ok=True)
    app_store = AppStore(app_db)
    win = Shell(app_store, projects_dir)

    def raise_window() -> None:
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.close()
        win.setWindowState(win.windowState() & ~Qt.WindowState.WindowMinimized)
        win.show()
        win.raise_()
        win.activateWindow()

    server.newConnection.connect(raise_window)
    win.show()
    try:
        return app.exec()
    finally:
        app_store.close()


if __name__ == "__main__":
    raise SystemExit(main())
