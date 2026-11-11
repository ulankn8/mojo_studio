import re
import shutil
import subprocess
from pathlib import Path

import pytest

import toystudio.app.main as app_main
from toystudio.app import i18n

APP_DIR = Path(i18n.__file__).resolve().parents[1]
VENV_BIN = Path(__file__).resolve().parents[2] / ".venv" / "bin"


def _missing_lrelease():
    raise RuntimeError("pyside6-lrelease not found")


@pytest.fixture
def no_lrelease(monkeypatch, tmp_path):
    monkeypatch.setattr(i18n, "_lrelease", _missing_lrelease)
    monkeypatch.setattr(i18n, "_DIR", tmp_path)  # fresh dir: no cached .qm
    (tmp_path / "ky.ts").write_text((APP_DIR / "i18n" / "ky.ts").read_text("utf-8"), "utf-8")


def test_install_language_without_lrelease_stays_ru(qapp, no_lrelease):
    assert i18n.install_language(qapp, "ky") is False
    assert i18n.install_language(qapp, "ru") is True


def test_shell_toggle_reverts_when_ky_unavailable(make_shell, no_lrelease):
    w = make_shell()
    assert w.set_language("ky") is False
    assert w.page_title() == "Проекты"
    assert w.top_bar._lang["ru"].isChecked()
    assert not w.top_bar._lang["ky"].isChecked()


def test_main_does_not_build_translations_at_startup(monkeypatch):
    monkeypatch.setattr(i18n, "_lrelease", _missing_lrelease)
    assert not hasattr(app_main, "build_translations")


def test_lrelease_lookup_tries_exe_and_scripts(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    exe = scripts / "pyside6-lrelease.exe"
    exe.write_text("")
    monkeypatch.setattr(i18n.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(i18n.shutil, "which", lambda _n: None)
    assert i18n._lrelease() == str(exe)


def test_lupdate_keeps_all_translations(tmp_path):
    lupdate = shutil.which("pyside6-lupdate") or str(VENV_BIN / "pyside6-lupdate")
    if not Path(lupdate).exists():
        pytest.skip("pyside6-lupdate not found")
    ts = tmp_path / "ky.ts"
    ts.write_text((APP_DIR / "i18n" / "ky.ts").read_text("utf-8"), "utf-8")
    sources = [str(p) for p in APP_DIR.rglob("*.py")]
    subprocess.run([lupdate, *sources, "-ts", str(ts)], check=True, capture_output=True)
    text = ts.read_text("utf-8")
    assert 'type="vanished"' not in text and 'type="obsolete"' not in text
    assert text.count("<message>") == 245
    assert "needs native-speaker review" in text
    assert "Долбоорлор" in text and "Кара көчүрмө" in text


def test_ky_ts_has_no_empty_context_and_no_untranslated_message():
    text = (APP_DIR / "i18n" / "ky.ts").read_text("utf-8")
    assert "<name></name>" not in text
    assert '<translation type="unfinished"></translation>' not in text


def test_every_draft_translation_is_flagged_for_review():
    text = (APP_DIR / "i18n" / "ky.ts").read_text("utf-8")
    for body in re.findall(r"<message>(.*?)</message>", text, re.DOTALL):
        source = re.search(r"<source>(.*?)</source>", body, re.DOTALL).group(1)
        if 'type="unfinished"' in body and source != "ru":  # "ru" is the language marker
            assert "needs native-speaker review" in body, source
