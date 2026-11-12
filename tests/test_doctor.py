"""The PC self-check: a sample render in an awkward folder, a Premiere kit, missing filters."""

from tests.media_fixtures import needs_ffmpeg
from toystudio import doctor


@needs_ffmpeg
def test_a_working_pc_passes_and_renders_the_sample():
    lines: list[str] = []
    assert doctor.run(None, lines.append) == 0
    assert any(line.startswith("✓ пробный рендер") and doctor.AWKWARD in line for line in lines)
    assert not any(line.startswith("✗") for line in lines)


@needs_ffmpeg
def test_premiere_kit(tmp_path):
    kit = tmp_path / "Проверка"
    assert doctor.run(kit, lambda _line: None) == 0
    xml = (kit / "T-Studio проверка.xml").read_text("utf-8")
    assert xml.count("<transitionitem>") >= 3 and "<compositemode>screen" in xml
    assert {p.name for p in kit.iterdir()} >= {"образец.mp4", "look.cube", "ПРОВЕРКА.txt"}


@needs_ffmpeg
def test_a_missing_filter_fails_and_skips_the_render(monkeypatch):
    monkeypatch.setattr(doctor, "has_filter", lambda name: name != "xfade")
    lines: list[str] = []
    assert doctor.run(None, lines.append) == 1
    assert any("нет xfade" in line for line in lines)
    assert any(line.startswith("✗ пробный рендер пропущен") for line in lines)


@needs_ffmpeg
def test_key_values_are_never_printed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    text = "\n".join(t for _, t in doctor.environment())
    assert "ключ задан в ANTHROPIC_API_KEY" in text and "secret" not in text
