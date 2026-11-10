"""Main window: top bar with the project stepper plus a stack of screens 1-3."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QSize
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from toystudio.app.i18n import install_language
from toystudio.app.platform.notify import Notifier
from toystudio.app.platform.taskbar import TaskbarProgress
from toystudio.app.screens.analysis import AnalysisScreen
from toystudio.app.screens.format_picker import FormatPickerScreen
from toystudio.app.screens.new_project import NewProjectScreen
from toystudio.app.screens.projects import ProjectsScreen
from toystudio.app.theme.apply import apply_theme
from toystudio.app.viewmodels.analysis_vm import AnalysisVM
from toystudio.app.viewmodels.new_project_vm import NewProjectVM
from toystudio.app.viewmodels.projects_vm import ProjectsVM
from toystudio.app.widgets.top_bar import TopBar
from toystudio.core.models import MediaKind, ProjectFormat
from toystudio.jobs.launch import stop_file_for
from toystudio.jobs.manager import JobManager
from toystudio.storage.app_store import AppStore
from toystudio.storage.project_store import ProjectStore

STEP_FILES, STEP_ANALYSIS = 0, 1
STOP_WAIT_S = 10.0
_KINDS = ("import", "analyze")
CloseChoice = Literal["minimize", "stop"] | None


def _job_root(job_id: str) -> tuple[str, Path] | None:
    kind, sep, root = job_id.partition(":")
    return (kind, Path(root)) if sep and kind in _KINDS else None


class Shell(QMainWindow):
    def __init__(
        self,
        app_store: AppStore,
        projects_dir: Path,
        *,
        jobs: JobManager | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        super().__init__()
        self.setMinimumSize(QSize(1280, 720))
        self.setWindowTitle("Той Студия")
        self.app_store = app_store
        self.projects_dir = projects_dir.resolve()
        self.jobs = jobs if jobs is not None else JobManager(max_parallel=1, parent=self)
        self.notifier = notifier or Notifier(QApplication.windowIcon())
        self.taskbar = TaskbarProgress(self)
        self.root_path: Path | None = None  # project open on screens 2-3
        self.new_project_screen: NewProjectScreen | None = None
        self.analysis_screen: AnalysisScreen | None = None
        # ruling R20: one VM per project with a queued or running analysis; it outlives
        # its screen and drives the card, the taskbar, the final status and the notification
        self.analyses: dict[Path, AnalysisVM] = {}
        self._closing = False

        root = QWidget()
        root.setObjectName("Root")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.top_bar = TopBar()
        lay.addWidget(self.top_bar)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        self.projects_vm = ProjectsVM(app_store, is_busy=self.is_busy)
        self.projects_vm.setParent(self)
        self.projects_screen = ProjectsScreen(self.projects_vm)
        self.stack.addWidget(self.projects_screen)
        self.projects_vm.open_requested.connect(self._open_project)
        self.format_picker = FormatPickerScreen()
        self.stack.addWidget(self.format_picker)
        self.format_picker.chosen.connect(
            lambda fmt: self.show_new_project(self._picked_files, fmt=fmt)
        )
        self._picked_files: list[Path] = []
        self.projects_vm.new_requested.connect(lambda files: self.show_format_picker(list(files)))

        self.top_bar.language_changed.connect(self._on_language)
        self.top_bar.theme_changed.connect(self._on_theme)
        self.top_bar.projects_requested.connect(self.show_projects)
        self.top_bar.step_clicked.connect(self._on_step)
        self.jobs.finished.connect(lambda _id: self._on_job_end())
        self.jobs.failed.connect(lambda _id, _code, _detail: self._on_job_end())
        self.show_projects()

    # ---- queries
    def current_screen(self) -> QWidget:
        return self.stack.currentWidget()

    def page_title(self) -> str:
        return self.projects_screen._title.text()

    def is_busy(self, root: Path) -> bool:
        """An import or analysis of the project runs or waits in the queue."""
        return any(self.jobs.is_active(f"{kind}:{root}") for kind in _KINDS)

    def analysis_running(self) -> bool:
        jobs = [*self.jobs.running(), *self.jobs.pending()]
        return any(j.startswith("analyze:") for j in jobs)

    # ---- navigation
    def show_projects(self) -> None:
        self._drop_new_project()
        self.root_path = None
        self.projects_vm.reload()
        self.stack.setCurrentWidget(self.projects_screen)
        self.top_bar.set_project_mode(False)

    def show_format_picker(self, files: list[Path] | None = None) -> None:
        """«Что монтируем?» before a new project; dropped files wait for the choice."""
        self._drop_new_project()
        self.root_path = None
        self._picked_files = list(files or [])
        self.stack.setCurrentWidget(self.format_picker)
        self.top_bar.set_project_mode(False)

    def show_new_project(
        self,
        files: list[Path] | None = None,
        root: Path | None = None,
        fmt: ProjectFormat = ProjectFormat.TOI,
    ) -> None:
        """Screen 2 for a new project (optionally with dropped files) or an existing one."""
        self._drop_new_project()
        self._picked_files = []
        vm = NewProjectVM(self.app_store, self.jobs, self.projects_dir, fmt)
        if root is not None:
            vm.attach(root.resolve())
        screen = NewProjectScreen(vm)
        vm.setParent(screen)
        screen.name_edit.blockSignals(True)
        screen.name_edit.setText(vm.name)
        screen.name_edit.blockSignals(False)
        vm.analysis_started.connect(self._on_analysis_started)
        self.new_project_screen = screen
        self.stack.addWidget(screen)
        self.stack.setCurrentWidget(screen)
        self.root_path = vm.root
        self.top_bar.set_project_mode(True)
        self._update_steps(STEP_FILES)
        vm.recheck()  # rows were computed before the screen connected
        if files:
            vm.add_files(MediaKind.VIDEO, files)
            self.root_path = vm.root

    def show_analysis(self, root: Path, *, assume_running: bool = False) -> None:
        root = root.resolve()
        self._drop_new_project()
        vm = self._analysis_vm(root, assume_running)
        screen = self.analysis_screen
        if screen is None or screen.vm is not vm:
            self._drop_analysis()
            screen = AnalysisScreen(vm, self.notifier)
            self.analysis_screen = screen
            self.stack.addWidget(screen)
        self.stack.setCurrentWidget(screen)
        self.root_path = root
        self.top_bar.set_project_mode(True)
        self._update_steps(STEP_ANALYSIS)

    def _open_project(self, root: Path, step: str) -> None:
        if step == "files":
            self.show_new_project(root=root)
        else:
            self.show_analysis(root)

    def _on_analysis_started(self, root: Path) -> None:
        self.show_analysis(root, assume_running=True)
        self.projects_vm.reload()

    def current_root(self) -> Path | None:
        """Project open on screens 2-3 (a new project gets its folder with the first file)."""
        screen = self.new_project_screen
        if screen is not None and self.current_screen() is screen:
            return screen.vm.root
        return self.root_path

    def _on_step(self, i: int) -> None:
        root = self.current_root()
        if root is not None and i == STEP_FILES and self._files_reachable(root):
            self.show_new_project(root=root)
        elif root is not None and i == STEP_ANALYSIS and self._analysis_reachable(root):
            self.show_analysis(root)
        else:  # the click moved the exclusive check mark; put it back
            on_analysis = self.current_screen() is self.analysis_screen
            self._update_steps(STEP_ANALYSIS if on_analysis else STEP_FILES)

    def _files_reachable(self, root: Path | None) -> bool:
        # editing the files of a project that is being analysed would race the worker
        return root is None or not self.jobs.is_active(f"analyze:{root}")

    def _analysis_reachable(self, root: Path | None) -> bool:
        if root is None:
            return False
        if self.jobs.is_active(f"analyze:{root}"):
            return True
        return ProjectStore.peek(root).step != "files"  # read-only: a worker may run

    def _update_steps(self, current: int) -> None:
        root = self.current_root()
        analysis = current == STEP_ANALYSIS or self._analysis_reachable(root)
        self.top_bar.set_step(current, STEP_ANALYSIS if analysis else STEP_FILES)
        self.top_bar.set_step_enabled(STEP_FILES, self._files_reachable(root))

    def _drop_new_project(self) -> None:
        screen, self.new_project_screen = self.new_project_screen, None
        if screen is None:
            return
        screen.vm.close()
        self.stack.removeWidget(screen)
        screen.deleteLater()

    def _drop_analysis(self) -> None:
        screen, self.analysis_screen = self.analysis_screen, None
        if screen is None:
            return
        self.stack.removeWidget(screen)
        screen.deleteLater()
        self._release(screen.vm)

    # ---- running analyses (R20): the registry, not the screen, follows every job
    def _analysis_vm(self, root: Path, assume_running: bool = False) -> AnalysisVM:
        vm = self.analyses.get(root)
        if vm is not None:
            return vm
        vm = AnalysisVM(root, self.jobs, self.app_store, assume_running=assume_running)
        vm.setParent(self)
        vm.changed.connect(lambda v=vm: self._on_analysis_changed(v))
        vm.finished.connect(lambda _cuts, v=vm: self._on_analysis_finished(v))
        if vm.running:
            self.analyses[root] = vm
        return vm

    def _release(self, vm: AnalysisVM) -> None:
        """Delete a VM that neither follows a job nor is on screen."""
        shown = self.analysis_screen is not None and self.analysis_screen.vm is vm
        if not shown and self.analyses.get(vm.root) is not vm:
            vm.deleteLater()

    def _on_analysis_changed(self, vm: AnalysisVM) -> None:
        if self._closing:
            return
        if vm.running:
            self.analyses.setdefault(vm.root, vm)  # resumed from screen 3
            self.projects_vm.set_progress(vm.root, round(vm.overall_percent), vm.remaining_s)
        else:  # the VM has written the final status to app.db
            if self.analyses.get(vm.root) is vm:
                del self.analyses[vm.root]
                self._release(vm)
            self.projects_vm.reload()
        self._update_taskbar()
        if self.current_screen() is self.analysis_screen:
            self._update_steps(STEP_ANALYSIS)

    def _update_taskbar(self) -> None:
        running = self.jobs.running()
        active = [vm for vm in self.analyses.values() if vm.running and vm.job_id in running]
        self.taskbar.set_progress(active[0].overall_percent if active else None)

    def _on_analysis_finished(self, vm: AnalysisVM) -> None:
        if self._closing or not vm.notify_when_done or vm.all_failed or vm.missing_files:
            return  # nothing to celebrate yet: screen 3 lists what went wrong
        self.notifier.notify(
            QCoreApplication.translate("AnalysisScreen", "Той Студия"),
            vm.finish_text(),
            lambda r=vm.root: self._open_from_notification(r),
        )

    def _open_from_notification(self, root: Path) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.show_analysis(root)

    def _on_job_end(self) -> None:
        if self.current_screen() is self.projects_screen and not self._closing:
            self.projects_vm.reload()

    # ---- closing while a worker runs
    def _ask_close(self) -> CloseChoice:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Той Студия")
        box.setText(
            QCoreApplication.translate(
                "Shell",
                "Анализ ещё идёт. Свернуть в трей или остановить? Пройденные этапы сохранятся.",
            )
        )
        minimize = box.addButton(
            QCoreApplication.translate("Shell", "Свернуть"), QMessageBox.ButtonRole.RejectRole
        )
        stop = box.addButton(
            QCoreApplication.translate("Shell", "Остановить и закрыть"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        box.setDefaultButton(minimize)
        box.exec()
        clicked = box.clickedButton()
        return "stop" if clicked is stop else "minimize" if clicked is minimize else None

    def stop_jobs(self, wait_s: float = STOP_WAIT_S) -> None:
        """Ask workers to stop via their stop files, wait for them, then kill what is left."""
        for job_id in self.jobs.pending():  # never started: nothing to save
            self.jobs.cancel(job_id)
        for job_id in self.jobs.running():
            parsed = _job_root(job_id)
            if parsed is None:
                continue
            stop = stop_file_for(parsed[1], parsed[0])
            with contextlib.suppress(OSError):
                stop.parent.mkdir(parents=True, exist_ok=True)
                stop.touch()
        end = time.monotonic() + wait_s
        while self.jobs.running() and time.monotonic() < end:
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            time.sleep(0.01)
        self.jobs.shutdown()

    def closeEvent(self, e: QCloseEvent) -> None:
        if self.analysis_running():
            choice = self._ask_close()
            if choice != "stop":
                e.ignore()
                if choice == "minimize":
                    self.notifier.minimize_to_tray(self)
                return
        self.stop_jobs()
        self._closing = True  # from here on nothing reads app.db: main closes it after exec()
        self._drop_new_project()
        self._drop_analysis()
        self.taskbar.set_progress(None)
        super().closeEvent(e)

    # ---- language and theme
    def set_language(self, code: Literal["ru", "ky"]) -> bool:
        ok = install_language(QApplication.instance(), code)  # type: ignore[arg-type]
        if not ok:
            code = "ru"  # translator unavailable: UI stays Russian, toggle must say so
        self.top_bar.set_language_checked(code)
        # installTranslator only posts LanguageChange; deliver it now so the switch is immediate.
        QApplication.sendEvent(self, QEvent(QEvent.Type.LanguageChange))
        return ok

    def _on_language(self, code: str) -> None:
        self.set_language("ky" if code == "ky" else "ru")

    def _on_theme(self, theme: str) -> None:
        apply_theme(QApplication.instance(), "light" if theme == "light" else "dark")  # type: ignore[arg-type]

    def retranslate(self) -> None:
        """Screens and the top bar retranslate themselves on LanguageChange."""

    def changeEvent(self, e: QEvent) -> None:
        if e.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(e)
