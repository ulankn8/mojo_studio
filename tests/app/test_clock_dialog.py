"""«Сверить часы камер»: a pair of clips corrects the camera, «Пропустить» saves nothing."""

from PySide6.QtCore import QThreadPool

from tests.jobs.test_day_draft import _project
from toystudio.app.widgets.clock_dialog import SKIPPED, ClockDialog
from toystudio.storage.project_store import ProjectStore

H = 3600 * 1_000_000


def _select(lst, name):
    for i in range(lst.count()):
        if lst.item(i).text().startswith(name):
            lst.item(i).setSelected(True)
            return
    raise AssertionError(name)


def test_pair_saves_the_correction_and_closes(qtbot, tmp_path):
    root = _project(tmp_path)  # the drone is 11 h behind
    d = ClockDialog(root)
    qtbot.addWidget(d)
    assert d.title.text() == "Часы камеры «дрон» не совпадают с остальными"
    assert d.cam_list.count() == 10 and d.ref_list.count() == 32
    assert not d.save_button.isEnabled()
    _select(d.cam_list, "DJI_0000.MP4")  # shot at minute 31 of the day
    _select(d.ref_list, "C0004.MP4")  # the stab's clip of minute 31
    assert d.save_button.isEnabled()
    with qtbot.waitSignal(d.accepted):
        d.save_button.click()
    QThreadPool.globalInstance().waitForDone(5000)
    s = ProjectStore.open(root)
    try:
        ((camera, offset),) = s.camera_clock().items()
        assert camera.endswith("дрон") and offset == 11 * H
    finally:
        s.close()


def test_skip_saves_nothing(qtbot, tmp_path):
    root = _project(tmp_path)
    d = ClockDialog(root)
    qtbot.addWidget(d)
    d.skip_button.click()
    QThreadPool.globalInstance().waitForDone(5000)
    assert d.result() == SKIPPED
    s = ProjectStore.open(root)
    try:
        assert s.camera_clock() == {}
    finally:
        s.close()


def test_nothing_to_match_accepts_at_once(qtbot, tmp_path):
    root = _project(tmp_path, drone_hours_off=0)
    d = ClockDialog(root)
    qtbot.addWidget(d)
    assert d.result() == 1  # QDialog.Accepted
