"""Shell wiring of screens 1-3, the stepper, app paths and closing while a worker runs."""

from datetime import date, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QMessageBox

from toystudio.app.main import app_paths
from toystudio.app.screens.analysis import AnalysisScreen
from toystudio.app.screens.new_project import NewProjectScreen
from toystudio.app.shell import Shell
from toystudio.app.viewmodels import new_project_vm as vm_mod
from toystudio.core.models import (
    MediaInfo,
    MediaKind,
    MediaRole,
    MediaStatus,
    ProjectFormat,
    StageName,
    StageState,
    ToiKind,
)
from toystudio.jobs.launch import stop_file_for
from toystudio.jobs.protocol import Event
from toystudio.storage.project_store import ProjectStore

GIB = 1024**3


class FakeJobs(QObject):
    """JobManager stand-in: a job "ends" as soon as its stop file exists (unless stubborn)."""

    progress = Signal(str, object)
    event = Signal(str, object)
    finished = Signal(str)
    failed = Signal(str, str, str)

    def __init__(self) -> None:
        super().__init__()
        self.active: list[str] = []
        self.queued: list[str] = []
        self.started: list[str] = []
        self.shutdowns = 0
        self.stubborn = False

    def start(self, job_id, argv):
        if job_id in self.active:
            raise ValueError(job_id)
        self.started.append(job_id)
        self.active.append(job_id)

    def cancel(self, job_id):
        if job_id in self.queued:
            self.queued.remove(job_id)
            self.failed.emit(job_id, "cancelled", "")
            return
        self.active = [j for j in self.active if j != job_id]

    def pending(self):
        return list(self.queued)

    def is_active(self, job_id):
        return job_id in self.running() or job_id in self.queued

    def finish(self, job_id):
        self.active.remove(job_id)
        self.finished.emit(job_id)

    def _stopped(self, job_id: str) -> bool:
        kind, _, root = job_id.partition(":")
        return not self.stubborn and stop_file_for(Path(root), kind).exists()

    def running(self):
        self.active = [j for j in self.active if not self._stopped(j)]
        return list(self.active)

    def shutdown(self):
        self.shutdowns += 1
        self.active.clear()
        self.queued.clear()


class FakeNotifier:
    def __init__(self) -> None:
        self.minimized = 0
        self.shown: list[tuple[str, str]] = []
        self.click = None

    def notify(self, title, body, on_click):
        self.shown.append((title, body))
        self.click = on_click

    def minimize_to_tray(self, window):
        self.minimized += 1


@pytest.fixture(autouse=True)
def _plenty_of_disk(monkeypatch):
    monkeypatch.setattr(vm_mod, "free_bytes", lambda p: 500 * GIB)
    monkeypatch.setattr(vm_mod, "is_removable", lambda p: False)


@pytest.fixture
def jobs():
    return FakeJobs()


@pytest.fixture
def shell(make_shell, jobs):
    return make_shell(jobs=jobs, notifier=FakeNotifier())


def _project(w, name: str, *, video: bool) -> Path:
    root = (w.projects_dir / name).resolve()
    store = ProjectStore.create(root, name, ToiKind.TOI, date(2026, 5, 1))
    if video:
        src = root.parent.parent / "src" / f"{name}.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"x" * 100)
        mid, _ = store.add_media(src, MediaKind.VIDEO, 100, False)
        store.set_stage(mid, StageName.PROBE, StageState.RUNNING)
    store.close()
    w.app_store.register_project(root, name, "analysis" if video else "files", datetime.now())
    w.projects_vm.reload()
    return root


class FakeTaskbar:
    def __init__(self) -> None:
        self.values: list[float | None] = []

    def set_progress(self, percent):
        self.values.append(percent)


def _finish_all_stages(root: Path) -> None:
    store = ProjectStore.open(root)  # test only: the fake worker "wrote" its results
    try:
        for m in store.list_media(MediaKind.VIDEO):
            for st in StageName:
                if st is not StageName.SYNC:
                    store.set_stage(m.id, st, StageState.DONE)
        store.set_stage(0, StageName.SYNC, StageState.DONE)
    finally:
        store.close()


def _card(w, root: Path):
    roots = w.projects_screen.card_roots()
    return w.projects_screen.card_at(roots.index(root))


def _stage_state(root: Path) -> StageState:
    ro = ProjectStore.open_readonly(root)
    try:
        return ro.stages()[0].state
    finally:
        ro.close()


# ---- app paths
def test_app_paths_windows():
    env = {"USERPROFILE": "/u/aibek", "APPDATA": "/u/aibek/AppData/Roaming"}
    projects, db = app_paths(env, "win32", Path("/ignored"))
    assert projects == Path("/u/aibek/Videos/T-Studio")
    assert db == Path("/u/aibek/AppData/Roaming/ToyStudio/app.db")


def test_app_paths_mac_and_linux():
    assert app_paths({}, "darwin", Path("/h")) == (
        Path("/h/Movies/T-Studio"),
        Path("/h/.toystudio/app.db"),
    )
    assert app_paths({}, "linux", Path("/h")) == (
        Path("/h/Videos/T-Studio"),
        Path("/h/.toystudio/app.db"),
    )


def test_app_paths_override_puts_both_under_home(tmp_path):
    projects, db = app_paths({"TOYSTUDIO_HOME": str(tmp_path)}, "win32", Path("/h"))
    assert projects.parent == tmp_path and db.parent == tmp_path


# ---- navigation
def test_starts_on_projects_without_stepper(shell):
    assert shell.current_screen() is shell.projects_screen
    assert all(b.isHidden() for b in shell.top_bar._steps)
    assert shell.top_bar._projects_btn.isHidden()


def test_new_project_and_back(shell):
    qtbot_click(shell.projects_screen.new_button)
    assert shell.current_screen() is shell.format_picker
    qtbot_click(shell.format_picker.buttons[ProjectFormat.WEDDING_FULL])
    assert isinstance(shell.current_screen(), NewProjectScreen)
    assert not shell.top_bar._steps[0].isHidden()
    assert shell.top_bar.step_enabled() == [True, False, False, False, False]
    assert shell.top_bar.current_step() == 0
    shell.top_bar.click_projects()
    assert shell.current_screen() is shell.projects_screen
    assert shell.new_project_screen is None


def qtbot_click(button):
    button.click()


def test_dropped_files_create_project_and_start_import(shell, jobs, tmp_path):
    clip = tmp_path / "в папке" / "клип 1.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"x" * 10)
    shell.projects_vm.request_new([clip])
    assert shell.current_screen() is shell.format_picker  # «Что монтируем?» comes first
    assert jobs.started == []
    shell.format_picker.buttons[ProjectFormat.TOI].click()
    screen = shell.new_project_screen
    assert screen is not None and shell.current_screen() is screen
    root = screen.vm.root
    assert root is not None and root.is_relative_to(shell.projects_dir)
    assert jobs.started == [f"import:{root}"]
    assert shell.is_busy(root)


def test_files_card_while_import_runs_reads_only(shell, jobs):
    root = _project(shell, "Той", video=True)
    jobs.active.append(f"import:{root}")
    shell._open_project(root, "files")
    screen = shell.new_project_screen
    assert screen is not None and shell.current_screen() is screen
    assert screen.vm._store is None
    assert screen.name_edit.text() == "Той"
    assert len(screen.video_zone.row_status_texts()) == 1
    assert _stage_state(root) is StageState.RUNNING  # open() would flip it to interrupted
    jobs.active.clear()
    jobs.finished.emit(f"import:{root}")
    assert screen.vm._store is not None


def test_files_card_without_job_opens_store(shell):
    root = _project(shell, "Пусто", video=False)
    shell.projects_vm.open(root)
    screen = shell.new_project_screen
    assert screen is not None and screen.vm._store is not None
    assert screen.vm.root == root


def test_attached_vm_queues_files_until_worker_ends(shell, jobs, tmp_path):
    root = _project(shell, "Той", video=True)
    jobs.active.append(f"import:{root}")
    shell._open_project(root, "files")
    vm = shell.new_project_screen.vm
    extra = tmp_path / "extra.mp4"
    extra.write_bytes(b"y" * 5)
    vm.add_files(MediaKind.VIDEO, [extra])
    assert len(vm.rows(MediaKind.VIDEO)) == 1
    jobs.active.clear()
    jobs.finished.emit(f"import:{root}")
    assert len(vm.rows(MediaKind.VIDEO)) == 2


def test_analysis_card_opens_screen_3(shell):
    root = _project(shell, "Той", video=True)
    shell.projects_vm.open(root)
    assert isinstance(shell.current_screen(), AnalysisScreen)
    assert shell.analysis_screen.vm.root == root
    assert shell.top_bar.current_step() == 1
    assert shell.top_bar.step_enabled() == [True, True, False, False, False]
    assert shell.analysis_screen.resume_button.isVisibleTo(shell)


def test_analysis_started_switches_and_progress_reaches_card(shell, jobs):
    root = _project(shell, "Той", video=True)
    jobs.active.append(f"analyze:{root}")
    shell.show_new_project(root=root)
    shell.new_project_screen.vm.analysis_started.emit(root)
    screen = shell.analysis_screen
    assert screen is not None and shell.current_screen() is screen
    assert shell.new_project_screen is None
    # files cannot be edited while this project is analysed
    assert shell.top_bar.step_enabled() == [False, True, False, False, False]
    shell.top_bar._steps[0].click()
    assert shell.current_screen() is screen and shell.top_bar.current_step() == 1
    jobs.event.emit(f"analyze:{root}", Event("eta", {"remaining_s": 30.0}))
    item = shell.projects_vm.item(root)
    assert item is not None and item.eta_s == 30.0 and item.percent is not None


def test_step_back_to_files_after_analysis_stopped(shell):
    root = _project(shell, "Той", video=True)
    shell.show_analysis(root)
    shell.top_bar._steps[0].click()
    assert isinstance(shell.current_screen(), NewProjectScreen)
    assert shell.top_bar.step_enabled()[:2] == [True, True]
    shell.top_bar._steps[1].click()
    assert isinstance(shell.current_screen(), AnalysisScreen)


# ---- closing
def test_close_without_jobs_just_closes(shell, jobs):
    shell._ask_close = lambda: pytest.fail("must not ask")
    shell.show()
    assert shell.close()
    assert jobs.shutdowns == 1


def test_close_during_analysis_minimize(shell, jobs):
    root = _project(shell, "Той", video=True)
    jobs.active.append(f"analyze:{root}")
    shell.show()
    shell._ask_close = lambda: "minimize"
    assert not shell.close()
    assert shell.notifier.minimized == 1
    assert jobs.shutdowns == 0 and jobs.running() == [f"analyze:{root}"]


def test_close_dialog_dismissed_keeps_window(shell, jobs):
    root = _project(shell, "Той", video=True)
    jobs.active.append(f"analyze:{root}")
    shell.show()
    shell._ask_close = lambda: None
    assert not shell.close()
    assert shell.notifier.minimized == 0 and jobs.shutdowns == 0


def test_close_during_analysis_stop_writes_stop_file_and_waits(shell, jobs):
    root = _project(shell, "Той", video=True)
    jobs.active.append(f"analyze:{root}")
    shell.show()
    shell._ask_close = lambda: "stop"
    assert shell.close()
    assert stop_file_for(root, "analyze").exists()
    assert jobs.shutdowns == 1


def test_stop_jobs_gives_up_after_the_wait(shell, jobs):
    root = _project(shell, "Той", video=True)
    jobs.active.append(f"analyze:{root}")
    jobs.stubborn = True
    shell.stop_jobs(wait_s=0.1)
    assert jobs.shutdowns == 1 and jobs.active == []


@pytest.mark.parametrize(("index", "expected"), [(0, "minimize"), (1, "stop")])
def test_close_question_texts_and_buttons(shell, monkeypatch, index, expected):
    seen = {}

    def fake_exec(box):
        seen["text"] = box.text()
        buttons = sorted(box.buttons(), key=lambda b: b.text() != "Свернуть")
        seen["labels"] = [b.text() for b in buttons]
        buttons[index].click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    assert Shell._ask_close(shell) == expected
    assert seen["text"] == (
        "Анализ ещё идёт. Свернуть в трей или остановить? Пройденные этапы сохранятся."
    )
    assert seen["labels"] == ["Свернуть", "Остановить и закрыть"]


def test_offscreen_smoke_show_and_close(make_shell, qtbot, qapp):
    w = make_shell()
    w.show()
    qapp.processEvents()
    w.set_language("ky")
    qapp.processEvents()
    assert w.page_title() == "Долбоорлор"
    assert w.close()


def test_mouse_click_on_card_opens_project(shell, qtbot):
    root = _project(shell, "Той", video=True)
    shell.show()
    card = shell.projects_screen.card_at(0)
    qtbot.waitUntil(lambda: card.height() > 0)
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert isinstance(shell.current_screen(), AnalysisScreen)
    assert shell.analysis_screen.vm.root == root


# ---- R20: running analyses survive navigation; queued jobs count as busy
def test_analysis_a_finishes_while_b_is_viewed(shell, jobs):
    shell.taskbar = FakeTaskbar()
    a = _project(shell, "А", video=True)
    b = _project(shell, "Б", video=True)
    jobs.active.append(f"analyze:{a}")
    shell.show_new_project(root=a)
    shell.new_project_screen.vm.analysis_started.emit(a)
    jobs.event.emit(f"analyze:{a}", Event("eta", {"remaining_s": 12.0}))
    assert shell.taskbar.values[-1] is not None
    shell.top_bar.click_projects()
    jobs.queued.append(f"analyze:{b}")
    shell.projects_vm.open(b)
    assert shell.analysis_screen.vm.root == b
    assert set(shell.analyses) == {a, b}
    _finish_all_stages(a)
    jobs.finish(f"analyze:{a}")
    assert set(shell.analyses) == {b}
    assert [r.status for r in shell.app_store.list_projects() if r.root == a] == ["analysis_done"]
    assert shell.notifier.shown == [("T-Studio", "Анализ «А» готов")]
    assert shell.taskbar.values[-1] is None
    shell.top_bar.click_projects()
    assert _card(shell, a)._badge.text() == "Анализ завершён"
    shell.notifier.click()
    assert shell.analysis_screen.vm.root == a and shell.analysis_screen.vm.done


def test_card_progress_follows_a_after_leaving_its_screen(shell, jobs):
    a = _project(shell, "А", video=True)
    jobs.active.append(f"analyze:{a}")
    shell.show_analysis(a)
    shell.top_bar.click_projects()
    assert shell.analysis_screen is not None  # the screen may go, the VM must stay
    shell.show_analysis(_project(shell, "Б", video=True))
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # dropped screens go
    jobs.event.emit(f"analyze:{a}", Event("eta", {"remaining_s": 7.0}))
    assert shell.projects_vm.item(a).eta_s == 7.0


def test_no_notification_when_unchecked(shell, jobs):
    a = _project(shell, "А", video=True)
    jobs.active.append(f"analyze:{a}")
    shell.show_analysis(a)
    shell.analysis_screen.notify_check.setChecked(False)
    shell.top_bar.click_projects()
    _finish_all_stages(a)
    jobs.finish(f"analyze:{a}")
    assert shell.notifier.shown == []


def test_queued_project_is_busy(shell, jobs, monkeypatch):
    a = _project(shell, "А", video=True)
    b = _project(shell, "Б", video=True)
    jobs.active.append(f"analyze:{a}")
    jobs.queued.append(f"analyze:{b}")
    assert shell.is_busy(b)
    assert shell.projects_vm.delete(b) is False and (b / "project.db").is_file()
    assert shell.projects_vm.rename(b, "Новое") is False
    shell.show_new_project(root=b)
    assert shell.new_project_screen.vm._store is None  # attached read-only
    assert _stage_state(b) is StageState.RUNNING
    shell.show_analysis(b)
    assert shell.analysis_screen.vm.running
    assert not shell.analysis_screen.resume_button.isVisibleTo(shell)


def test_close_stop_cancels_queued_analysis(shell, jobs):
    a = _project(shell, "А", video=True)
    b = _project(shell, "Б", video=True)
    jobs.active.append(f"analyze:{a}")
    jobs.queued.append(f"analyze:{b}")
    shell.show()
    assert shell.close()
    assert jobs.queued == [] and jobs.shutdowns == 1


# ---- final review fixes
def _unstarted(w, name: str, *, probed: bool = True, copied: bool = False) -> Path:
    """A project with one video added on screen 2 whose analysis never started."""
    root = (w.projects_dir / name).resolve()
    store = ProjectStore.create(root, name, ToiKind.TOI, date(2026, 5, 1))
    src = root.parent.parent / "src" / f"{name}.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x" * 100)
    mid, _ = store.add_media(src, MediaKind.VIDEO, 100, copied)
    if probed:
        store.set_media_info(
            mid, MediaInfo(5_000_000, 1920, 1080, 0, 25.0, "h264", False, True, True, 1, None)
        )
    store.close()
    w.app_store.register_project(root, name, "files", datetime.now())
    w.projects_vm.reload()
    return root


def test_unstarted_project_reopens_on_screen_2(shell, jobs):
    # ruling R21: files added, analysis never started -> the card opens screen 2
    root = _unstarted(shell, "Той")
    shell.projects_vm.open(root)
    assert isinstance(shell.current_screen(), NewProjectScreen)
    assert shell.top_bar.current_step() == 0
    assert jobs.started == []  # nothing left to import
    assert shell.new_project_screen.vm.can_start


def test_reopened_project_restarts_interrupted_import(shell, jobs):
    root = _unstarted(shell, "Той", probed=False)
    shell.projects_vm.open(root)
    assert jobs.started == [f"import:{root}"]
    assert not shell.new_project_screen.vm.can_start


def test_reopened_project_restarts_unfinished_copy(shell, jobs):
    root = _unstarted(shell, "Той", copied=True)  # probed, but the copy was cut off
    shell.projects_vm.open(root)
    assert jobs.started == [f"import:{root}"]


def test_reopened_project_rechecks_missing_file_that_is_back(shell, jobs):
    root = _unstarted(shell, "Той")
    store = ProjectStore.open(root)
    store.set_media_status(1, MediaStatus.MISSING)
    store.close()
    shell.projects_vm.open(root)
    assert jobs.started == [f"import:{root}"]


def test_no_import_restart_while_its_worker_runs(shell, jobs):
    root = _unstarted(shell, "Той", probed=False)
    jobs.active.append(f"import:{root}")
    shell.projects_vm.open(root)
    assert jobs.started == []


def test_no_ready_notification_while_a_file_is_missing(shell, jobs):
    a = _project(shell, "А", video=True)
    jobs.active.append(f"analyze:{a}")
    shell.show_analysis(a)
    _finish_all_stages(a)
    store = ProjectStore.open(a)
    store.set_media_status(1, MediaStatus.MISSING)
    store.close()
    jobs.finish(f"analyze:{a}")
    assert shell.notifier.shown == []
    assert shell.analysis_screen.missing_label.isVisibleTo(shell)
    assert [r.status for r in shell.app_store.list_projects() if r.root == a] == ["analysis"]


# --- «Что монтируем?» and tracks ---


def _src(tmp_path: Path, name: str) -> Path:
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 10)
    return p


def _pick(shell, fmt: ProjectFormat, files: list[Path] | None = None):
    shell.projects_vm.request_new(files or [])
    shell.format_picker.buttons[fmt].click()
    return shell.new_project_screen


def test_picker_offers_every_format(shell):
    shell.projects_vm.request_new([])
    assert shell.current_screen() is shell.format_picker
    assert set(shell.format_picker.buttons) == set(ProjectFormat)
    assert shell.format_picker.buttons[ProjectFormat.WEDDING_FULL].text() == "Полный фильм"
    assert shell.new_project_screen is None  # nothing is created before the choice


def test_full_wedding_shows_five_tracks_in_order(shell):
    screen = _pick(shell, ProjectFormat.WEDDING_FULL)
    assert list(screen.zones) == [
        MediaRole.PRE,
        MediaRole.MAIN,
        MediaRole.CUTAWAY,
        MediaRole.SPEECH,
        MediaRole.MUSIC,
    ]
    labels = [label.text() for label in screen._track_labels.values()]
    assert labels[0].startswith("До ресторана") and labels[1] == "Ресторан: основная камера"
    assert not any(chip.isVisibleTo(screen) for chip in screen.chips.values())
    assert screen._format_label.text() == "Свадьба · полный фильм"


def test_express_has_shooting_and_music_only(shell):
    screen = _pick(shell, ProjectFormat.WEDDING_EXPRESS)
    assert list(screen.zones) == [MediaRole.PRE, MediaRole.MUSIC]
    assert screen.music_zone.folder_button.isVisibleTo(screen.music_zone)
    assert not screen.zones[MediaRole.PRE].folder_button.isVisibleTo(screen.zones[MediaRole.PRE])


def test_files_dropped_in_a_track_keep_its_role(shell, jobs, tmp_path):
    screen = _pick(shell, ProjectFormat.WEDDING_FULL)
    screen.zones[MediaRole.SPEECH].files_dropped.emit([_src(tmp_path, "071424-011212.WAV")])
    screen.zones[MediaRole.CUTAWAY].files_dropped.emit([_src(tmp_path, "C0001.MP4")])
    store = ProjectStore.open_readonly(screen.vm.root)
    try:
        assert store.format is ProjectFormat.WEDDING_FULL
        roles = {m.path.name: m.role for m in store.list_media()}
        assert roles == {"071424-011212.WAV": MediaRole.SPEECH, "C0001.MP4": MediaRole.CUTAWAY}
        assert ProjectStore.peek(screen.vm.root).kind is ToiKind.WEDDING
    finally:
        store.close()
    assert len(screen.zones[MediaRole.SPEECH].row_status_texts()) == 1
    assert screen.zones[MediaRole.MAIN].row_status_texts() == []


def test_files_dropped_on_projects_go_to_the_first_video_track(shell, tmp_path):
    screen = _pick(shell, ProjectFormat.WEDDING_FULL, [_src(tmp_path, "DJI_0565_D.MP4")])
    assert len(screen.zones[MediaRole.PRE].row_status_texts()) == 1
    assert screen.zones[MediaRole.MAIN].row_status_texts() == []


def test_reopened_project_shows_its_tracks(shell, tmp_path):
    screen = _pick(shell, ProjectFormat.PODCAST)
    screen.zones[MediaRole.SPEECH].files_dropped.emit([_src(tmp_path, "mic.wav")])
    root = screen.vm.root
    shell.show_projects()
    shell.show_new_project(root=root)
    again = shell.new_project_screen
    assert list(again.zones) == [
        MediaRole.MAIN,
        MediaRole.CUTAWAY,
        MediaRole.SPEECH,
        MediaRole.MUSIC,
    ]
    assert len(again.zones[MediaRole.SPEECH].row_status_texts()) == 1


def test_old_projects_open_as_toi_with_kind_chips(shell):
    root = _project(shell, "Той", video=True)
    shell.show_new_project(root=root)
    screen = shell.new_project_screen
    assert list(screen.zones) == [MediaRole.MAIN, MediaRole.MUSIC]
    assert screen.chips[ToiKind.TOI].isVisibleTo(screen)
    assert screen._track_labels == {}  # the layout before formats: no track headers


def test_picker_and_tracks_in_kyrgyz(shell):
    assert shell.set_language("ky")
    shell.projects_vm.request_new([])
    assert shell.format_picker._title.text() != "Что монтируем?"
    screen = _pick(shell, ProjectFormat.WEDDING_FULL)
    assert screen._track_labels[MediaRole.MUSIC].text() != "Библиотека музыки"
