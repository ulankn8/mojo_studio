"""End-to-end flow of phase 2a: projects -> new project -> analysis, with the real worker."""

import time

import pytest
from PySide6.QtCore import Qt

from tests.media_fixtures import make_clip, make_toast_clip, needs_ffmpeg
from toystudio.app.main import app_paths
from toystudio.app.shell import Shell
from toystudio.core.models import ProjectFormat, StageName
from toystudio.jobs.protocol import Progress
from toystudio.storage.app_store import AppStore
from toystudio.storage.project_store import ProjectStore

FLOW_TIMEOUT_S = 120


class Budget:
    """One deadline for the whole flow; each wait gets what is left of it."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def ms(self) -> int:
        return max(1, int((self._end - time.monotonic()) * 1000))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TOYSTUDIO_HOME", str(tmp_path / "Той Студия home"))
    return tmp_path


class FakeNotifier:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def notify(self, title, body, on_click):
        self.shown.append((title, body))

    def minimize_to_tray(self, window):
        pass


def _shell(qtbot, app_store, projects_dir, notifier=None) -> Shell:
    w = Shell(app_store, projects_dir, notifier=notifier)
    w._ask_close = lambda: "stop"  # a failed test must not block on the close question
    qtbot.addWidget(w)
    w.show()
    return w


def _clips(tmp_path, duration_s: float):
    a = make_clip(tmp_path / "clips" / "a.mp4", duration_s=duration_s, size="320x180")
    b = make_clip(
        tmp_path / "Той Айбек и Айгуль" / "клип 2.mp4",
        duration_s=duration_s,
        size="320x180",
        tone_hz=660,
    )
    return a, b


def _add_files_and_start(qtbot, w: Shell, files, budget: Budget) -> None:
    qtbot.mouseClick(w.projects_screen.new_button, Qt.MouseButton.LeftButton)
    assert w.current_screen() is w.format_picker
    qtbot.mouseClick(w.format_picker.buttons[ProjectFormat.TOI], Qt.MouseButton.LeftButton)
    np = w.new_project_screen
    assert np is not None and w.current_screen() is np
    assert w.top_bar.step_enabled()[2:] == [False, False, False]
    np.name_edit.setText("Свадьба Айбек")
    np.video_zone.files_dropped.emit(list(files))
    qtbot.waitUntil(
        lambda: np.video_zone.row_status_texts() == ["Готово ✓"] * len(files), timeout=budget.ms()
    )
    qtbot.waitUntil(np.start_button.isEnabled, timeout=budget.ms())
    qtbot.mouseClick(np.start_button, Qt.MouseButton.LeftButton)


@needs_ffmpeg
def test_new_project_to_finished_analysis(qtbot, home):
    budget = Budget(FLOW_TIMEOUT_S)
    projects_dir, app_db = app_paths()
    assert projects_dir.is_relative_to(home)
    a, b = _clips(home, 6)
    app_store = AppStore(app_db)
    try:
        w = _shell(qtbot, app_store, projects_dir)
        assert w.current_screen() is w.projects_screen
        assert w.projects_screen.vm.total == 0
        assert w.projects_screen.empty_state.isVisible()

        _add_files_and_start(qtbot, w, [a, b], budget)
        screen = w.analysis_screen
        assert screen is not None and w.current_screen() is screen
        assert w.top_bar.current_step() == 1
        with qtbot.waitSignal(screen.vm.finished, timeout=budget.ms()):
            pass
        assert screen.vm.done
        assert [v.state for v in screen.vm.stages] == ["done"] * 5
        assert screen.done_label.isVisible()

        w.top_bar.click_projects()
        assert w.current_screen() is w.projects_screen
        assert w.projects_screen.card_roots() == [screen.vm.root]
        card = w.projects_screen.card_at(0)
        assert card._badge.text() == "Анализ завершён"
        assert card._pixmap is not None and not card._pixmap.isNull()
        assert ProjectStore.peek(screen.vm.root).step == "analysis_done"
    finally:
        app_store.close()


@needs_ffmpeg
def test_analysis_finds_toast_and_applause(qtbot, home):
    budget = Budget(FLOW_TIMEOUT_S)
    projects_dir, app_db = app_paths()
    clip = make_toast_clip(home)
    app_store = AppStore(app_db)
    notifier = FakeNotifier()
    try:
        w = _shell(qtbot, app_store, projects_dir, notifier)
        _add_files_and_start(qtbot, w, [clip], budget)
        screen = w.analysis_screen
        assert screen is not None and w.current_screen() is screen
        with qtbot.waitSignal(screen.vm.finished, timeout=budget.ms()):
            pass
        assert screen.vm.done
        screen.flush_feed()

        by_stage = {s.key: s.state for s in screen.vm.stages}
        assert by_stage[StageName.SPEECH.value] == "done"
        assert by_stage[StageName.MOMENTS.value] == "done"
        types = [c.type_text() for c in screen.feed_cards()]
        assert "Тост" in types and "Аплодисменты" in types, types
        assert screen.vm.moment_total >= 2

        assert len(notifier.shown) == 1
        body = notifier.shown[0][1]
        assert "найдено" in body and str(screen.vm.moment_total) in body, body
    finally:
        app_store.close()


@needs_ffmpeg
def test_stop_close_reopen_resume(qtbot, home):
    budget = Budget(FLOW_TIMEOUT_S)
    projects_dir, app_db = app_paths()
    a, b = _clips(home, 20)
    app_store = AppStore(app_db)
    try:
        w = _shell(qtbot, app_store, projects_dir)
        proxy_seen: list[str] = []
        w.jobs.progress.connect(
            lambda job_id, msg: (
                proxy_seen.append(job_id)
                if isinstance(msg, Progress) and msg.stage == StageName.PROXY.value
                else None
            )
        )
        _add_files_and_start(qtbot, w, [a, b], budget)
        root = w.analysis_screen.vm.root
        qtbot.waitUntil(lambda: bool(proxy_seen), timeout=budget.ms())

        # close while the analysis runs: «Остановить и закрыть»
        asked: list[bool] = []
        w._ask_close = lambda: asked.append(True) or "stop"
        assert w.close()
        assert asked == [True]
        assert w.jobs.running() == []
        assert ProjectStore.peek(root).step == "analysis"

        w2 = _shell(qtbot, app_store, projects_dir)
        assert w2.projects_screen.card_roots() == [root]
        w2.projects_screen.vm.open(root)
        screen = w2.analysis_screen
        assert screen is not None and w2.current_screen() is screen
        assert screen.resume_button.isVisible()
        assert screen.resume_button.text() == "Продолжить анализ"
        with qtbot.waitSignal(screen.vm.finished, timeout=budget.ms()):
            qtbot.mouseClick(screen.resume_button, Qt.MouseButton.LeftButton)
        assert screen.vm.done
        assert ProjectStore.peek(root).step == "analysis_done"
    finally:
        app_store.close()
