from datetime import date

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from toystudio.app.screens.analysis import AnalysisScreen
from toystudio.app.theme.apply import apply_theme
from toystudio.app.viewmodels.analysis_vm import AnalysisVM
from toystudio.core.models import (
    MediaInfo,
    MediaKind,
    MediaStatus,
    MomentRow,
    MomentType,
    ProjectFormat,
    QualityInterval,
    QualityLabel,
    Reason,
    SpeechRegion,
    StageName,
    StageState,
    ToiKind,
)
from toystudio.jobs.launch import stop_file_for
from toystudio.jobs.protocol import Event, Progress
from toystudio.storage.app_store import AppStore
from toystudio.storage.project_store import ProjectStore


class FakeJobs(QObject):
    progress = Signal(str, object)
    event = Signal(str, object)
    finished = Signal(str)
    failed = Signal(str, str, str)

    def __init__(self) -> None:
        super().__init__()
        self.started: list[tuple[str, list[str]]] = []
        self.cancelled: list[str] = []
        self.active: list[str] = []
        self.queued: list[str] = []

    def start(self, job_id, argv):
        self.started.append((job_id, argv))
        self.active.append(job_id)

    def cancel(self, job_id):
        self.cancelled.append(job_id)

    def running(self):
        return list(self.active)

    def pending(self):
        return list(self.queued)

    def is_active(self, job_id):
        return job_id in self.active or job_id in self.queued


class FakeNotifier:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []
        self.minimized = 0
        self.click = None

    def notify(self, title, body, on_click):
        self.shown.append((title, body))
        self.click = on_click

    def minimize_to_tray(self, window):
        self.minimized += 1


def info(duration_s=60) -> MediaInfo:
    return MediaInfo(
        duration_us=duration_s * 1_000_000,
        width=1920,
        height=1080,
        rotation=0,
        fps=25.0,
        codec="h264",
        hdr=False,
        has_audio=True,
        has_video=True,
        bit_rate=8_000_000,
        creation_time=None,
    )


@pytest.fixture
def app_store(tmp_path):
    s = AppStore(tmp_path / "app.db")
    yield s
    s.close()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "Той Студия" / "Свадьба Айбек и Нурайым"
    store = ProjectStore.create(
        root, "Свадьба Айбек и Нурайым", ToiKind.WEDDING, date(2026, 10, 12)
    )
    ids = []
    for name in ("IMG_0001.MP4", "IMG_0419.MP4"):
        mid, _ = store.add_media(tmp_path / name, MediaKind.VIDEO, 1000, False)
        store.set_media_info(mid, info())
        ids.append(mid)
    yield root, store, ids
    store.close()


@pytest.fixture
def jobs():
    return FakeJobs()


def make_vm(project, jobs, app_store, running=True, delay=10_000):
    root, _, _ = project
    app_store.register_project(root, "Свадьба", "analysis", __import__("datetime").datetime.now())
    if running:
        jobs.active.append(f"analyze:{root}")
    vm = AnalysisVM(root, jobs, app_store, stop_delay_ms=delay)
    return vm


def job_id(project):
    return f"analyze:{project[0]}"


def set_all(store, ids, stage, state, found=None):
    for mid in ids:
        store.set_stage(mid, stage, state)


def finish_stage(store, ids, stage):
    set_all(store, ids, stage, StageState.DONE)


def emit_stage(jobs, project, mid, stage, state, found=None):
    jobs.event.emit(
        job_id(project),
        Event(
            "stage",
            {"media_id": mid, "stage": stage.value, "state": state.value, "found": found},
        ),
    )


def test_stage_texts_and_waiting_states(qtbot, project, jobs, app_store):
    apply_theme(QApplication.instance(), "dark")
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    titles = [s.title for s in vm.stages]
    assert titles == [
        "Готовим копию для анализа",
        "Слушаем речь и ищем тосты",
        "Находим смены сцен",
        "Убираем размытые и тёмные кадры",
        "Ищем танцы, аплодисменты, музыку",
    ]
    assert [s.state for s in vm.stages] == ["waiting"] * 5  # speech and moments are real stages
    assert screen.stage_widgets[1].found_label.isHidden()
    assert screen.stage_widgets[4].found_label.isHidden()


def test_found_texts_after_stages_done(qtbot, project, jobs, app_store):
    _root, store, ids = project
    for st in (StageName.PROBE, StageName.PROXY, StageName.SCENES, StageName.QUALITY):
        finish_stage(store, ids, st)
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    store.replace_scene_cuts(ids[0], [1, 2, 3], [0.5, 0.5, 0.5])
    store.replace_scene_cuts(ids[1], [4], [0.5])
    store.replace_quality(ids[1], [QualityInterval(ids[1], 0, 40_000_000, QualityLabel.DARK)])
    vm = make_vm(project, jobs, app_store)
    texts = [s.found_text for s in vm.stages]
    assert texts[0] == "Готово: 2 файла"
    assert texts[2] == "Найдено 4 смены сцен"
    assert texts[3] == "Чётко: 1 файл · темновато: 1"
    assert [s.state for s in vm.stages] == ["done", "waiting", "done", "done", "waiting"]


def test_progress_events_update_overall_and_eta(qtbot, project, jobs, app_store):
    _root, store, ids = project
    vm = make_vm(project, jobs, app_store)
    assert vm.overall_percent == 0
    assert vm.eta_text is None
    finish_stage(store, ids, StageName.PROBE)
    store.set_stage(ids[0], StageName.PROXY, StageState.RUNNING)
    emit_stage(jobs, project, ids[0], StageName.PROXY, StageState.RUNNING)
    jobs.progress.emit(job_id(project), Progress("proxy", 30, 60, str(ids[0])))
    first = vm.overall_percent
    assert first > 0
    jobs.progress.emit(job_id(project), Progress("proxy", 60, 60, str(ids[0])))
    assert vm.overall_percent > first
    assert vm.stages[0].state == "running"
    assert vm.stages[0].percent is not None
    jobs.event.emit(job_id(project), Event("eta", {"remaining_s": 23 * 60 - 10}))
    assert vm.eta_text == "ещё ~23 мин"


def test_progress_of_other_job_is_ignored(qtbot, project, jobs, app_store):
    vm = make_vm(project, jobs, app_store)
    jobs.progress.emit("analyze:other", Progress("proxy", 30, 60, "1"))
    jobs.event.emit("analyze:other", Event("eta", {"remaining_s": 100}))
    assert vm.overall_percent == 0
    assert vm.eta_text is None


def test_stop_creates_stop_file_and_shows_resume(qtbot, project, jobs, app_store):
    root, _, _ = project
    vm = make_vm(project, jobs, app_store, delay=50)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    screen.show()
    assert screen.resume_button.isHidden()
    screen.stop_button.click()
    assert stop_file_for(root, "analyze").exists()
    # the worker did not stop by itself: cancel after the delay
    qtbot.waitUntil(lambda: jobs.cancelled == [job_id(project)], timeout=2000)
    jobs.failed.emit(job_id(project), "cancelled", "")
    assert not stop_file_for(root, "analyze").exists()
    assert not screen.resume_button.isHidden()
    assert not screen.saved_label.isHidden()
    assert screen.saved_label.text() == "Пройденные этапы сохранены"
    assert screen.stop_button.isHidden()


def test_stop_without_cancel_when_worker_ends_in_time(qtbot, project, jobs, app_store):
    vm = make_vm(project, jobs, app_store, delay=100)
    vm.stop()
    jobs.failed.emit(job_id(project), "stopped", "")
    qtbot.wait(300)
    assert jobs.cancelled == []


def test_resume_restarts_analyze(qtbot, project, jobs, app_store):
    _root, _, _ = project
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.can_resume
    vm.resume()
    assert jobs.started[-1][0] == job_id(project)
    assert "analyze" in jobs.started[-1][1]
    assert not vm.can_resume
    assert vm.running


def test_finished_screen_shows_done_and_status(qtbot, project, jobs, app_store):
    _root, store, ids = project
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    for st in (*[s for s in StageName if s not in (StageName.SYNC,)],):
        finish_stage(store, ids, st)
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    finished = []
    vm.finished.connect(finished.append)
    jobs.active.clear()
    jobs.finished.emit(job_id(project))
    assert finished == [0]
    assert app_store.list_projects()[0].status == "analysis_done"
    assert not screen.done_label.isHidden()


def test_notify_checkbox_drives_vm(qtbot, project, jobs, app_store):
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    assert screen.notify_check.isChecked() and vm.notify_when_done
    screen.notify_check.setChecked(False)
    assert vm.notify_when_done is False


def test_stop_of_queued_job_cancels_it(qtbot, project, jobs, app_store):
    jobs.queued.append(job_id(project))
    vm = AnalysisVM(project[0], jobs, app_store)
    assert vm.running and vm.can_resume is False
    vm.stop()
    assert jobs.cancelled == [job_id(project)]
    assert not stop_file_for(project[0], "analyze").exists()


def test_minimize_goes_to_tray(qtbot, project, jobs, app_store):
    notifier = FakeNotifier()
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=notifier)
    qtbot.addWidget(screen)
    screen.minimize_button.click()
    assert notifier.minimized == 1


def test_taskbar_noop_off_windows(qtbot):
    from PySide6.QtWidgets import QWidget

    from toystudio.app.platform.taskbar import TaskbarProgress

    w = QWidget()
    qtbot.addWidget(w)
    tb = TaskbarProgress(w)
    for v in (None, 0, 37.5, 100):
        tb.set_progress(v)


def test_notifier_noop_without_tray(qtbot, monkeypatch):
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QSystemTrayIcon, QWidget

    from toystudio.app.platform.notify import Notifier

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    n = Notifier(QIcon())
    n.notify("Той Студия", "x", lambda: None)
    w = QWidget()
    qtbot.addWidget(w)
    w.show()
    n.minimize_to_tray(w)
    assert w.isMinimized()


def test_reopen_interrupted_project_shows_resume(qtbot, project, jobs, app_store):
    _root, store, ids = project
    finish_stage(store, ids, StageName.PROBE)
    store.set_stage(ids[0], StageName.PROXY, StageState.INTERRUPTED)
    vm = make_vm(project, jobs, app_store, running=False)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    screen.show()
    assert not screen.resume_button.isHidden()
    assert screen.resume_button.text() == "Продолжить анализ"
    assert screen.stop_button.isHidden()
    assert vm.stages[0].state == "waiting"
    # opening is read-only: the interrupted row is untouched and nothing is started
    assert store.stage(ids[0], StageName.PROXY).state is StageState.INTERRUPTED
    assert jobs.started == []


def test_stale_running_row_shows_as_waiting_when_no_job(qtbot, project, jobs, app_store):
    _root, store, ids = project
    store.set_stage(ids[0], StageName.PROBE, StageState.RUNNING)
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.stages[0].state == "waiting"


def test_failed_file_listed_and_retry(qtbot, project, jobs, app_store):
    _root, store, ids = project
    for st in (StageName.PROBE, StageName.PROXY, StageName.SCENES, StageName.QUALITY):
        store.set_stage(ids[0], st, StageState.DONE)
    store.set_stage(ids[1], StageName.PROBE, StageState.DONE)
    store.set_stage(ids[1], StageName.PROXY, StageState.FAILED, error_code="ffmpeg")
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.failed_files == ["IMG_0419.MP4"]
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    screen.show()
    assert screen.failed_label.text() == "Не удалось проанализировать: IMG_0419.MP4"
    assert screen.error_banner.isHidden()
    screen.retry_button.click()
    assert jobs.started and jobs.started[-1][0] == job_id(project)


def test_every_file_failed_shows_banner(qtbot, project, jobs, app_store):
    _root, store, ids = project
    for mid in ids:
        store.set_stage(mid, StageName.PROBE, StageState.DONE)
        store.set_stage(mid, StageName.PROXY, StageState.FAILED, error_code="ffmpeg")
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.all_failed
    assert vm.stages[0].state == "done" or vm.stages[0].state == "error"
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    screen.show()
    assert not screen.error_banner.isHidden()
    assert "Ни один файл не удалось проанализировать." in screen.error_banner._title.text()
    assert vm.stages[2].state == "error"


def test_job_failure_is_reported(qtbot, project, jobs, app_store):
    vm = make_vm(project, jobs, app_store)
    seen = []
    vm.failed.connect(lambda c, d: seen.append((c, d)))
    jobs.failed.emit(job_id(project), "crashed", "boom")
    assert seen == [("crashed", "boom")]
    assert not vm.running


def test_current_language_tracks_install(qapp):
    from toystudio.app.i18n import current_language, install_language

    assert current_language() == "ru"
    assert install_language(qapp, "ky")
    assert current_language() == "ky"
    install_language(qapp, "ru")
    assert current_language() == "ru"


def test_eta_text_in_kyrgyz(qtbot, qapp, project, jobs, app_store):
    from toystudio.app.i18n import install_language

    install_language(qapp, "ky")
    vm = make_vm(project, jobs, app_store)
    jobs.event.emit(job_id(project), Event("eta", {"remaining_s": 600}))
    assert vm.eta_text == "дагы ~10 мүн"


def test_old_crash_banner_does_not_return_after_resume_and_stop(qtbot, project, jobs, app_store):
    vm = make_vm(project, jobs, app_store, delay=50)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    screen.show()
    jobs.failed.emit(job_id(project), "crashed", "boom")
    assert not screen.error_banner.isHidden()
    screen.error_banner.action.emit("retry")  # «Повторить»
    assert vm.running
    assert screen.error_banner.isHidden()
    vm.stop()
    jobs.failed.emit(job_id(project), "stopped", "")
    assert screen.error_banner.isHidden()
    assert not screen.resume_button.isHidden()


# ---- final review fixes
def _all_done(store, mid):
    for st in (StageName.PROBE, StageName.PROXY, StageName.SCENES, StageName.QUALITY):
        store.set_stage(mid, st, StageState.DONE)
    for st in (StageName.SPEECH, StageName.MOMENTS):
        store.set_stage(mid, st, StageState.SKIPPED)


def test_missing_file_is_listed_and_blocks_done(qtbot, project, jobs, app_store):
    _root, store, ids = project
    _all_done(store, ids[0])
    store.set_stage(ids[1], StageName.PROBE, StageState.PENDING)
    store.set_media_status(ids[1], MediaStatus.MISSING)
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.missing_files == ["IMG_0419.MP4"]
    assert not vm.done
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    screen.show()
    assert screen.missing_label.isVisible()
    assert screen.missing_label.text() == (
        "Файл не найден: IMG_0419.MP4. Подключите диск с ним или замените файл."
    )
    assert screen.done_label.isHidden()
    screen.missing_retry_button.click()  # the drive is back: analyze picks the file up
    assert jobs.started and jobs.started[-1][0] == job_id(project)


def test_corrupt_only_failure_hides_retry(qtbot, project, jobs, app_store):
    _root, store, ids = project
    _all_done(store, ids[0])
    store.set_media_status(ids[1], MediaStatus.CORRUPT)
    store.set_stage(ids[1], StageName.PROBE, StageState.FAILED, error_code="corrupt")
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.failed_files == ["IMG_0419.MP4"] and not vm.failed_retryable
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    screen.show()
    assert screen.retry_button.isHidden()
    assert screen.replace_hint.isVisible()
    assert screen.replace_hint.text() == "Замените файл на шаге «Файлы»."


def test_retry_shown_when_a_failure_can_be_retried(qtbot, project, jobs, app_store):
    _root, store, ids = project
    store.set_media_status(ids[0], MediaStatus.CORRUPT)
    store.set_stage(ids[0], StageName.PROBE, StageState.FAILED, error_code="corrupt")
    store.set_stage(ids[1], StageName.PROBE, StageState.DONE)
    store.set_stage(ids[1], StageName.PROXY, StageState.FAILED, error_code="ffmpeg")
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.failed_retryable


def test_failed_sync_still_finishes(qtbot, project, jobs, app_store):
    _root, store, ids = project
    for mid in ids:
        _all_done(store, mid)
    store.set_stage(0, StageName.SYNC, StageState.FAILED, error_code="error")
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.done
    assert vm.stages[2].state == "done"
    assert vm.overall_percent == 100.0


def test_notifier_null_icon_falls_back_to_app_icon(qtbot):
    from PySide6.QtGui import QIcon

    from toystudio.app.platform.notify import Notifier
    from toystudio.app.widgets.icons import app_icon

    assert not app_icon().isNull()
    assert not app_icon().pixmap(256, 256).isNull()
    n = Notifier(QIcon())
    assert not n.icon.isNull()


def test_tray_menu_open_and_quit(qtbot):
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QSystemTrayIcon, QWidget

    from toystudio.app.platform.notify import Notifier

    n = Notifier(QIcon())
    assert [a.text() for a in n.menu.actions()] == ["Открыть", "Выйти"]
    w = QWidget()
    qtbot.addWidget(w)
    n._window = w  # as after minimize_to_tray
    n.open_action.trigger()
    assert w.isVisible()
    w.hide()
    n._tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert w.isVisible()
    n.quit_action.trigger()
    assert not w.isVisible()


# ---- phase 2b: moment feed, stage texts
def _row(mid, kind=MomentType.TOAST, start=10, quality=QualityLabel.CLEAR):
    return MomentRow(
        0,
        mid,
        kind,
        start * 1_000_000,
        (start + 20) * 1_000_000,
        0.9,
        quality,
        (Reason("toast", "Звучит тост", "Тост угулду"),),
    )


def _store_moments(project, rows_by_media, with_previews=True):
    """Write moments (and preview files) the way the worker does; returns ids in order."""
    root, store, _ = project
    out = []
    for mid, rows in rows_by_media:
        ids = store.replace_moments(mid, rows)
        pairs = []
        for i in ids:
            rel = f"cache/thumbs/m{i}.jpg"
            if with_previews:
                from PySide6.QtGui import QImage

                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                img = QImage(32, 18, QImage.Format.Format_RGB32)
                img.fill(0x336699)
                img.save(str(root / rel))
            pairs.append((i, rel))
        store.set_previews(pairs)
        out.extend(ids)
    return out


def _emit_moment(
    jobs, project, mid, moment_id, kind="toast", start=10, quality="clear", reasons=None
):
    jobs.event.emit(
        job_id(project),
        Event(
            "moment",
            {
                "media_id": mid,
                "moment_id": moment_id,
                "type": kind,
                "start_us": start * 1_000_000,
                "end_us": (start + 20) * 1_000_000,
                "score": 0.9,
                "preview": f"cache/thumbs/m{moment_id}.jpg",
                "quality_label": quality,
                "reasons": [{"code": "toast", "ru": "Звучит тост", "ky": "Тост угулду"}]
                if reasons is None
                else reasons,
            },
        ),
    )


def test_feed_adds_cards_from_events_newest_first(qtbot, project, jobs, app_store):
    _root, _store, ids = project
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    assert screen.feed_cards() == []
    assert screen.found_empty.isVisible() or not screen.found_empty.isHidden()
    mids = _store_moments(
        project,
        [(ids[0], [_row(ids[0], start=10), _row(ids[0], MomentType.DANCE, start=100)])],
    )
    _emit_moment(jobs, project, ids[0], mids[0])
    _emit_moment(jobs, project, ids[0], mids[1], "dance", 100)
    _emit_moment(jobs, project, ids[0], mids[1], "dance", 100)  # duplicate event
    screen.flush_feed()
    cards = screen.feed_cards()
    assert [c.type_text() for c in cards] == ["Танец", "Тост"]
    assert cards[0].has_preview()
    assert cards[1].reason_text() == "Звучит тост"
    assert screen.found_empty.isHidden()


def test_feed_caps_at_50(qtbot, project, jobs, app_store):
    _root, _store, ids = project
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    rows = [_row(ids[0], start=i * 30) for i in range(53)]
    mids = _store_moments(project, [(ids[0], rows)])
    for i, m in enumerate(mids):
        _emit_moment(jobs, project, ids[0], m, start=i * 30)
    screen.flush_feed()
    assert len(screen.feed_cards()) == 50
    assert screen.more_text() == "и ещё 3"
    _emit_moment(jobs, project, ids[0], mids[-1] + 1)
    screen.flush_feed()
    assert screen.more_text() == "и ещё 4"


def test_feed_loaded_from_store_on_reopen(qtbot, project, jobs, app_store):
    _root, _store, ids = project
    mids = _store_moments(
        project,
        [(ids[0], [_row(ids[0], start=10)]), (ids[1], [_row(ids[1], MomentType.MUSIC, 5)])],
    )
    vm = make_vm(project, jobs, app_store, running=False)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    assert [c.type_text() for c in screen.feed_cards()] == ["Музыка", "Тост"]
    assert mids[0] < mids[1]
    assert screen.more_text() == ""


def test_feed_reloads_on_moments_stage_done(qtbot, project, jobs, app_store):
    _root, _store, ids = project
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    mids = _store_moments(project, [(ids[0], [_row(ids[0]), _row(ids[0], start=90)])])
    _emit_moment(jobs, project, ids[0], mids[1], start=90)  # the first event was lost
    screen.flush_feed()
    assert len(screen.feed_cards()) == 1
    emit_stage(jobs, project, ids[0], StageName.MOMENTS, StageState.DONE)
    assert len(screen.feed_cards()) == 2


def test_feed_missing_preview_shows_placeholder(qtbot, project, jobs, app_store):
    _root, _store, ids = project
    _store_moments(project, [(ids[0], [_row(ids[0])])], with_previews=False)
    vm = make_vm(project, jobs, app_store, running=False)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    (card,) = screen.feed_cards()
    assert not card.has_preview()


def test_stage_texts_use_counts(qtbot, project, jobs, app_store):
    _root, store, ids = project
    for st in (StageName.SPEECH, StageName.MOMENTS):
        finish_stage(store, ids, st)
    store.replace_audio(ids[0], [SpeechRegion(0, 1_000_000, 0.9)] * 12, [])
    rows0 = [_row(ids[0], start=i * 30) for i in range(14)]
    rows0 += [_row(ids[0], MomentType.DANCE, 500 + i * 30) for i in range(9)]
    rows1 = [_row(ids[1], MomentType.MUSIC, 5)]
    _store_moments(project, [(ids[0], rows0), (ids[1], rows1)])
    vm = make_vm(project, jobs, app_store)
    assert vm.stages[1].state == "done"
    assert vm.stages[1].found_text == "Найдено 12 участков речи"
    assert vm.stages[4].found_text == "Найдено 14 тостов · 9 танцев · 1 музыка"
    store.replace_audio(ids[0], [SpeechRegion(0, 1_000_000, 0.9)] * 21, [])
    vm._reload()
    vm._compute()
    assert vm.stages[1].found_text == "Найден 21 участок речи"


def test_notification_mentions_moments(qtbot, project, jobs, app_store):
    _root, store, ids = project
    for st in StageName:
        if st is not StageName.SYNC:
            finish_stage(store, ids, st)
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    _store_moments(project, [(ids[0], [_row(ids[0]), _row(ids[0], start=90)])])
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.finish_text() == "Анализ «Свадьба Айбек и Нурайым» готов — найдено 2 момента"
    _store_moments(project, [(ids[1], [_row(ids[1], start=i * 30) for i in range(3)])])
    vm._reload()
    assert vm.finish_text().endswith("найдено 5 моментов")
    store.replace_moments(ids[0], [])
    store.replace_moments(ids[1], [_row(ids[1])])
    vm._reload()
    assert vm.finish_text().endswith("найден 1 момент")
    store.replace_moments(ids[1], [])
    vm._reload()
    assert vm.finish_text() == "Анализ «Свадьба Айбек и Нурайым» готов"


def test_feed_retranslates_to_ky(qtbot, qapp, project, jobs, app_store):
    from toystudio.app.i18n import install_language

    _root, _store, ids = project
    _store_moments(project, [(ids[0], [_row(ids[0], MomentType.DANCE)])])
    vm = make_vm(project, jobs, app_store, running=False)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    assert install_language(qapp, "ky")
    qapp.processEvents()
    try:
        (card,) = screen.feed_cards()
        assert card.type_text() != "Танец"
        assert card.reason_text() == "Тост угулду"
    finally:
        install_language(qapp, "ru")
    qapp.processEvents()
    assert screen.feed_cards()[0].reason_text() == "Звучит тост"


def test_event_payload_reasons_and_quality_reach_the_card(qtbot, project, jobs, app_store):
    _root, _store, ids = project
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    _emit_moment(jobs, project, ids[0], 7, quality="dark")
    screen.flush_feed()
    (card,) = screen.feed_cards()
    assert card.quality_text() == "темновато"
    assert card.reason_text() == "Звучит тост"


def test_burst_of_300_events_renders_incrementally_without_store(
    qtbot, project, jobs, app_store, monkeypatch
):
    _root, _store, ids = project
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    opened = []
    real = ProjectStore.open_readonly
    monkeypatch.setattr(
        ProjectStore, "open_readonly", classmethod(lambda c, r: opened.append(r) or real(r))
    )
    rebuilds = screen.feed_rebuilds
    for i in range(1, 301):
        _emit_moment(jobs, project, ids[0], i, start=i)
    assert opened == []  # events carry everything the card shows
    assert len(screen.feed_cards()) < 300
    qtbot.waitUntil(lambda: len(screen.feed_cards()) == 50, timeout=2000)
    assert screen.feed_rebuilds - rebuilds <= 2
    assert screen.more_text() == "и ещё 250"
    assert len(vm.feed) == 300 and vm.feed[0].id == 300
    _emit_moment(jobs, project, ids[0], 300)  # duplicate
    assert len(vm.feed) == 300


# ---- whole-branch review fixes (phase 2b)
def test_speech_failure_lists_file_with_retry(qtbot, project, jobs, app_store):
    _root, store, ids = project
    _all_done(store, ids[0])
    for st in (StageName.PROBE, StageName.PROXY):
        store.set_stage(ids[1], st, StageState.DONE)
    store.set_stage(ids[1], StageName.SPEECH, StageState.FAILED, error_code="error")
    for st in (StageName.SCENES, StageName.QUALITY, StageName.MOMENTS):
        store.set_stage(ids[1], st, StageState.PENDING)
    store.set_stage(0, StageName.SYNC, StageState.RUNNING)
    vm = make_vm(project, jobs, app_store)
    assert vm.failed_files == ["IMG_0419.MP4"]
    assert vm.failed_retryable
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    jobs.active.clear()
    jobs.finished.emit(job_id(project))
    assert vm.done
    assert "waiting" not in [s.state for s in vm.stages]
    assert vm.overall_percent == 100.0
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    screen.show()
    assert screen.failed_label.text() == "Не удалось проанализировать: IMG_0419.MP4"
    assert not screen.retry_button.isHidden()


def test_speech_progress_moves_stage_percent(qtbot, project, jobs, app_store):
    _root, store, ids = project
    for mid in ids:
        for st in (StageName.PROBE, StageName.PROXY):
            store.set_stage(mid, st, StageState.DONE)
    store.set_stage(ids[0], StageName.SPEECH, StageState.RUNNING)
    vm = make_vm(project, jobs, app_store)
    assert vm.stages[1].state == "running"
    before_stage, before_all = vm.stages[1].percent, vm.overall_percent
    jobs.progress.emit(job_id(project), Progress("speech", 30, 60, str(ids[0])))
    assert vm.stages[1].percent == pytest.approx(before_stage + 0.25)
    assert vm.overall_percent > before_all
    emit_stage(jobs, project, ids[0], StageName.SPEECH, StageState.INTERRUPTED)
    assert (ids[0], StageName.SPEECH) not in vm._live


def test_speech_row_says_no_sound_when_every_file_is_silent(qtbot, project, jobs, app_store):
    _root, store, ids = project
    for mid in ids:
        _all_done(store, mid)  # SPEECH SKIPPED everywhere: no audio track at all
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    vm = make_vm(project, jobs, app_store, running=False)
    assert vm.stages[1].state == "done"
    assert vm.stages[1].found_text == "Звука нет"
    store.set_stage(ids[0], StageName.SPEECH, StageState.DONE)
    vm._reload()
    vm._compute()
    assert vm.stages[1].found_text == "Найдено 0 участков речи"


# --- draft for Premiere after the analysis ---


def _finish(store, ids, jobs, project):
    for mid in ids:
        _all_done(store, mid)
    store.set_stage(0, StageName.SYNC, StageState.DONE)
    jobs.active.clear()
    jobs.finished.emit(job_id(project))


def _set_format(root, fmt):
    import sqlite3

    con = sqlite3.connect(str(root / "project.db"))
    con.execute("UPDATE project SET format=?", (fmt.value,))
    con.commit()
    con.close()


def test_draft_button_only_for_restaurant_formats_when_done(qtbot, project, jobs, app_store):
    root, store, ids = project
    _set_format(root, ProjectFormat.WEDDING_FULL)
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    assert screen._draft_box.isHidden()  # still running
    _finish(store, ids, jobs, project)
    assert not screen._draft_box.isHidden()
    visible = [k for k, b in screen.draft_buttons.items() if not b.isHidden()]
    assert visible == ["film", "restaurant", "sde_ai"]
    assert screen.draft_buttons["film"].text() == "Черновик до ресторана"
    assert screen.draft_buttons["restaurant"].text() == "Черновик ресторана"
    assert screen.open_folder_button.isHidden() and screen.draft_label.text() == ""


def test_no_draft_button_for_toi(qtbot, project, jobs, app_store):
    _root, store, ids = project
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    _finish(store, ids, jobs, project)
    assert not screen.done_label.isHidden() and screen._draft_box.isHidden()


def test_draft_result_and_error_texts(qtbot, project, jobs, app_store, monkeypatch, tmp_path):
    from toystudio.app.viewmodels import analysis_vm as avm
    from toystudio.engine.edit.restaurant import DraftStats
    from toystudio.jobs.restaurant_draft import DraftError, DraftResult

    root, store, ids = project
    _set_format(root, ProjectFormat.PODCAST)
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    _finish(store, ids, jobs, project)

    def fail(_root):
        raise DraftError("no_synced_cameras")

    monkeypatch.setattr(avm, "build_draft", fail)
    screen.draft_buttons["restaurant"].click()
    assert screen.draft_label.text().startswith("Камеры не совпали со звуком")
    assert screen.open_folder_button.isHidden()
    stats = DraftStats(duration_us=9_070_000_000, cuts=1480, dropped_us=2_280_000_000, blocks=12)
    result = DraftResult(tmp_path / "export" / "x.xml", "cut", stats)
    monkeypatch.setattr(avm, "build_draft", lambda _root: result)
    screen.draft_buttons["restaurant"].click()
    assert (
        screen.draft_label.text() == "Черновик: 02:31:10, склеек 1480, убрано пауз и музыки 38:00"
    )
    assert not screen.open_folder_button.isHidden()


def test_draft_buttons_by_format(qtbot, project, jobs, app_store):
    root, store, ids = project
    for fmt, kinds, text in (
        (ProjectFormat.WEDDING_EXPRESS, ["express", "sde_ai"], "Собрать экспресс-видео"),
        (ProjectFormat.LOVE_STORY, ["sde_ai", "sde"], "Love Story с ИИ"),
        (ProjectFormat.PODCAST, ["restaurant"], "Собрать черновик для Premiere"),
    ):
        _set_format(root, fmt)
        vm = make_vm(project, jobs, app_store, running=False)
        screen = AnalysisScreen(vm, notifier=FakeNotifier())
        qtbot.addWidget(screen)
        _finish(store, ids, jobs, project)
        vm._reload()
        vm._compute()
        screen.retranslate()
        screen._render()
        visible = [k for k, b in screen.draft_buttons.items() if not b.isHidden()]
        assert visible == kinds and screen.draft_buttons[kinds[0]].text() == text


def test_wrong_clocks_open_the_dialog_then_retry(qtbot, project, jobs, app_store, monkeypatch):
    from toystudio.app.screens import analysis as screen_mod
    from toystudio.app.viewmodels import analysis_vm as avm
    from toystudio.jobs.restaurant_draft import DraftError

    root, store, ids = project
    _set_format(root, ProjectFormat.WEDDING_FULL)
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    _finish(store, ids, jobs, project)
    calls = []

    def fake_day(_root, kind, *, skip_clocks=False, effects=True):
        calls.append((kind, skip_clocks))
        raise DraftError("clocks" if not skip_clocks else "nothing_to_keep")

    class FakeDialog:
        def __init__(self, *_a):
            pass

        def result(self):
            return 0

        def exec(self):
            return screen_mod.SKIPPED

    monkeypatch.setattr(avm, "build_day_draft", fake_day)
    monkeypatch.setattr(screen_mod, "ClockDialog", FakeDialog)
    screen.draft_buttons["film"].click()
    assert calls == [("film", False), ("film", True)]  # asked, the user skipped, built anyway
    assert screen.draft_label.text().startswith("В записи не нашлось")


def _ai_screen(qtbot, project, jobs, app_store, monkeypatch, plan_result):
    from toystudio.app.viewmodels import analysis_vm as avm
    from toystudio.jobs.day_draft import LoveInputs

    root, store, ids = project
    _set_format(root, ProjectFormat.LOVE_STORY)
    vm = make_vm(project, jobs, app_store)
    screen = AnalysisScreen(vm, notifier=FakeNotifier())
    qtbot.addWidget(screen)
    _finish(store, ids, jobs, project)
    monkeypatch.setattr(
        avm,
        "love_inputs",
        lambda _root, skip_clocks=False: LoveInputs([], 98.0, 612_000, 88_000_000),
    )
    monkeypatch.setattr(avm, "plan_love_story", plan_result)
    monkeypatch.setattr(vm, "needs_clocks", lambda: False)
    return vm, screen


def test_ai_love_story_runs_in_background_and_shows_the_story(
    qtbot, project, jobs, app_store, monkeypatch, tmp_path
):
    from toystudio.app.viewmodels import analysis_vm as avm
    from toystudio.core.ai_plan import AiPlan
    from toystudio.engine.edit.restaurant import DraftStats
    from toystudio.jobs.restaurant_draft import DraftResult

    plan = AiPlan("Сборы, встреча, крыша", (), "claude-opus-5-5", 21000, 7000, 106)
    vm, screen = _ai_screen(qtbot, project, jobs, app_store, monkeypatch, lambda *a, **k: plan)
    stats = DraftStats(duration_us=88_000_000, cuts=47, dropped_us=0, blocks=1)
    result = DraftResult(tmp_path / "export" / "x.xml", "cut", stats)
    monkeypatch.setattr(avm, "build_day_draft", lambda *a, **k: result)
    screen.draft_buttons["sde_ai"].click()
    assert vm.ai_busy and screen.draft_label.text().startswith("ИИ смотрит кадры")
    assert not screen.draft_buttons["sde"].isEnabled()
    qtbot.waitUntil(lambda: not vm.ai_busy, timeout=5000)
    first, look = screen.draft_label.text().split("\n")
    assert first == "Love Story: 01:28, склеек 47 · ИИ: «Сборы, встреча, крыша» · ≈ $0.22"
    assert "Тёплое кино.cube" in look and "Lumetri" in look
    assert screen.draft_buttons["sde"].isEnabled() and not screen.open_folder_button.isHidden()
    assert not screen.render_button.isHidden() and screen.render_button.text() == "Сохранить видео"

    # the finished video, in the background, from the same draft
    from toystudio.jobs.day_draft import RenderResult

    calls = []

    def fake_render(root, kind, **kw):
        calls.append((kind, kw["skip_clocks"]))
        kw["on_progress"](0.5)
        return RenderResult(tmp_path / "export" / "x.mp4", from_proxy=True)

    monkeypatch.setattr(avm, "render_draft", fake_render)
    screen.render_button.click()
    assert vm.render_busy and screen.render_button.text().startswith("Сохраняю видео")
    qtbot.waitUntil(lambda: not vm.render_busy, timeout=5000)
    assert calls == [("sde_ai", False)]
    text = screen.draft_label.text()
    assert "Видео сохранено: x.mp4" in text and "уменьшенных копий" in text


def test_render_failure_is_explained_and_rules_drafts_can_render(
    qtbot, project, jobs, app_store, monkeypatch, tmp_path
):
    from toystudio.app.viewmodels import analysis_vm as avm
    from toystudio.engine.edit.restaurant import DraftStats
    from toystudio.jobs.restaurant_draft import DraftError, DraftResult

    plan = None
    vm, screen = _ai_screen(qtbot, project, jobs, app_store, monkeypatch, lambda *a, **k: plan)
    stats = DraftStats(duration_us=88_000_000, cuts=47, dropped_us=0, blocks=1)
    monkeypatch.setattr(avm, "build_day_draft",
                        lambda *a, **k: DraftResult(tmp_path / "x.xml", "cut", stats))  # fmt: skip
    screen.draft_buttons["sde"].click()
    assert vm.can_render and not screen.render_button.isHidden()

    def broken(*_a, **_k):
        raise DraftError("missing_files")

    monkeypatch.setattr(avm, "render_draft", broken)
    screen.render_button.click()
    qtbot.waitUntil(lambda: not vm.render_busy, timeout=5000)
    assert "подключите диск" in screen.draft_label.text()


def test_ai_without_a_key_explains_what_to_do(qtbot, project, jobs, app_store, monkeypatch):
    from toystudio.cloud.director import DirectorError

    def no_key(*_a, **_k):
        raise DirectorError("no_key")

    vm, screen = _ai_screen(qtbot, project, jobs, app_store, monkeypatch, no_key)
    screen.draft_buttons["sde_ai"].click()
    qtbot.waitUntil(lambda: not vm.ai_busy, timeout=5000)
    assert "console.anthropic.com" in screen.draft_label.text()
    assert "setx ANTHROPIC_API_KEY" in screen.draft_label.text()
    assert screen.open_folder_button.isHidden()


def test_light_effects_switch_reaches_drafts_and_videos(
    qtbot, project, jobs, app_store, monkeypatch, tmp_path
):
    from toystudio.app.viewmodels import analysis_vm as avm
    from toystudio.engine.edit.restaurant import DraftStats
    from toystudio.jobs.day_draft import RenderResult
    from toystudio.jobs.restaurant_draft import DraftResult

    vm, screen = _ai_screen(qtbot, project, jobs, app_store, monkeypatch, lambda *a, **k: None)
    assert not screen.effects_check.isHidden() and screen.effects_check.isChecked()
    seen = []
    stats = DraftStats(duration_us=88_000_000, cuts=47, dropped_us=0, blocks=1)

    def fake_build(_root, kind, **kw):
        seen.append(("draft", kw["effects"]))
        return DraftResult(tmp_path / "x.xml", "cut", stats)

    def fake_render(_root, kind, **kw):
        seen.append(("video", kw["effects"]))
        return RenderResult(tmp_path / "x.mp4", from_proxy=False)

    monkeypatch.setattr(avm, "build_day_draft", fake_build)
    monkeypatch.setattr(avm, "render_draft", fake_render)
    screen.effects_check.setChecked(False)
    screen.draft_buttons["sde"].click()
    screen.render_button.click()
    qtbot.waitUntil(lambda: not vm.render_busy, timeout=5000)
    assert seen == [("draft", False), ("video", False)]
