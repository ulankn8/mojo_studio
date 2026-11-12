"""Self-check of a PC for T-Studio: `python -m toystudio.doctor [--premiere FOLDER]`.

Offline and free: Python and its packages, ffmpeg with the filters the renderer needs, whether
the AI keys are set (never their values), and a sample render through the real renderer in a
folder with an awkward name (Cyrillic, a comma, an apostrophe, brackets). With --premiere the
sample's clips, its Premiere XML, the look LUT and a checklist are left in FOLDER. Exit code 0
when nothing required is missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from toystudio.core.models import US_PER_S
from toystudio.engine.color.grade import write_cube
from toystudio.engine.edit.overlays import OVERLAYS, asset
from toystudio.engine.edit.restaurant import Item
from toystudio.engine.export.premiere_xml import FileRef, write_sequence
from toystudio.engine.media.ffmpeg import ffmpeg_major, find_tool, has_filter, popen_flags
from toystudio.engine.media.probe import probe_media
from toystudio.engine.render.film import RenderSettings, render_film

FILTERS = ("lut3d", "xfade", "cas", "loudnorm", "zoompan", "rotate", "vignette", "blend", "amix")
GPU_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox")
KEYS = {"ИИ-режиссёр (Claude)": ("ANTHROPIC_API_KEY",),
        "Музыка ИИ (Lyria)": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}  # fmt: skip
AWKWARD = "Тест T-Studio, O'Brien [1]"
S = US_PER_S
SAMPLE_US = 7 * S
FPS = 25

Check = tuple[bool | None, str]  # True ok, False missing (required), None for information


def _ffmpeg(args: list[str]) -> str:
    proc = subprocess.run([find_tool("ffmpeg"), "-hide_banner", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False,
                          stdin=subprocess.DEVNULL, creationflags=popen_flags())  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-500:])
    return proc.stdout


def environment() -> list[Check]:
    out: list[Check] = [(sys.version_info >= (3, 12), f"Python {platform.python_version()}")]
    for name, module in (("PySide6", "PySide6"), ("anthropic", "anthropic"),
                         ("google-genai", "google.genai")):  # fmt: skip
        out.append((importlib.util.find_spec(module) is not None, f"пакет {name}"))
    try:
        out.append((True, f"ffmpeg: {find_tool('ffmpeg')}"))
        out.append((True, f"ffprobe: {find_tool('ffprobe')}"))
    except FileNotFoundError:
        return [*out, (False, "ffmpeg/ffprobe не найдены (PATH или TOYSTUDIO_FFMPEG_DIR)")]
    out.append((ffmpeg_major() >= 7, f"версия ffmpeg {ffmpeg_major()} (нужна 7 или новее)"))
    missing = [f for f in FILTERS if not has_filter(f)]
    out.append((not missing, "фильтры ffmpeg: " + (f"нет {', '.join(missing)}" if missing
                                                   else "все на месте")))  # fmt: skip
    encoders = _ffmpeg(["-encoders"])
    out.append(("libx264" in encoders, "кодировщик libx264"))
    gpu = [e for e in GPU_ENCODERS if e in encoders]
    out.append((None, "видеокарта для кодирования: " + (", ".join(gpu) if gpu else "нет")))
    for what, names in KEYS.items():
        found = next((n for n in names if os.environ.get(n)), None)
        out.append((None, f"{what}: ключ {'задан в ' + found if found else 'не задан'}"))
    return out


def _clip(path: Path, hue: int, seconds: float) -> None:
    _ffmpeg(["-y", "-f", "lavfi", "-i", f"testsrc2=s=1280x720:r={FPS}:d={seconds}",
             "-f", "lavfi", "-i", f"sine=frequency={440 + hue}:d={seconds}",
             "-vf", f"hue=h={hue},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
             "-c:a", "aac", "-shortest", str(path)])  # fmt: skip


def sample(folder: Path) -> tuple[list[Item], dict[int, Path], dict[int, str], Path]:
    """Three 8 s test clips and a tone in `folder`: a 7 s film that uses every trick of the
    renderer and the XML - a dissolve, slow motion, a push-in, a flash to white, a whip, a
    light overlay and music. Returns (items, sources, transitions, the look LUT)."""
    folder.mkdir(parents=True, exist_ok=True)
    sources: dict[int, Path] = {}
    for mid, hue in ((1, 0), (2, 120), (3, 240)):
        sources[mid] = folder / f"план {mid}.mp4"
        _clip(sources[mid], hue, 8)
    sources[9] = folder / "музыка.m4a"
    _ffmpeg(["-y", "-f", "lavfi", "-i", "sine=frequency=330:d=8", "-c:a", "aac", str(sources[9])])
    sweep, sweep_us = OVERLAYS["sweep"]
    sources[sweep] = asset(sweep)
    items = [
        Item("V1", 1, S // 2, S // 2 + 2 * S, 0, 2 * S),
        Item("V1", 2, S // 2, S // 2 + S, 2 * S, 4 * S, speed=0.5),
        Item("V1", 3, S // 2, S // 2 + 2 * S, 4 * S, 6 * S, move="in"),
        Item("V1", 1, 4 * S, 5 * S, 6 * S, SAMPLE_US),
        Item("V2", sweep, 0, sweep_us, S, S + sweep_us, blend="screen", opacity=0.85),
        Item("M", 9, 0, SAMPLE_US, 0, SAMPLE_US),
    ]
    transitions = {2 * S: "fade", 4 * S: "white", 6 * S: "whip"}
    return items, sources, transitions, write_cube(folder / "look.cube")


CHECKLIST = """Проверка T-Studio в Premiere

Откройте «T-Studio проверка.xml» (File → Import) и откройте последовательность.
Рядом лежит «образец.mp4» — так ролик должен выглядеть после программы.

1. Медиа подключилось само: нет красных клипов «Media Offline».
2. На V1 четыре плана; на 0:02, 0:04 и 0:06 стоят переходы Cross Dissolve.
3. Второй план (0:02–0:04): скорость 50 % (правый клик → Speed/Duration).
4. Третий план (0:04–0:06): в Effect Controls → Motion есть ключи Scale (медленный наезд).
5. На V2 около 0:01 — световая накладка; режим наложения Screen, картинка видна сквозь неё.
6. Под роликом музыка (ровный тон), с начала до конца.
7. Цвет: Lumetri Color → Creative → Look → Browse → look.cube — картинка теплеет.

Ответ по каждому пункту — «да» или «нет»; где «нет», пришлите скриншот.
"""


def render_sample(folder: Path, premiere: bool) -> list[Check]:
    items, sources, transitions, lut = sample(folder)
    durations = {mid: probe_media(p).duration_us for mid, p in sources.items()}
    out = render_film(items, sources, durations, folder / "образец.mp4",
                      luts={1: lut, 2: lut, 3: lut}, transitions=transitions,
                      settings=RenderSettings.preview(), work=folder / "render")  # fmt: skip
    got = probe_media(out).duration_us
    checks: list[Check] = [(abs(got - SAMPLE_US) <= S // 10,
                            f"пробный рендер в папке «{folder.name}»: {got / S:.2f} с из 7")]  # fmt: skip
    if premiere:
        files = {}
        for mid, p in sources.items():
            info = probe_media(p)
            files[mid] = FileRef(str(p), info.duration_us, FPS, info.width, info.height,
                                 info.width > 0, 2 if info.has_audio else 0)  # fmt: skip
        xml = folder / "T-Studio проверка.xml"
        write_sequence(xml, "T-Studio проверка", items, files, FPS, 1920, 1080,
                       transitions=transitions, fade=True)  # fmt: skip
        (folder / "ПРОВЕРКА.txt").write_text(CHECKLIST, encoding="utf-8")
        checks.append((True, f"набор для Premiere: {folder}"))
    return checks


def run(premiere: Path | None, say: Callable[[str], None] = print) -> int:
    checks = environment()
    if all(ok is not False for ok, _ in checks):
        try:
            if premiere is not None:
                checks += render_sample(premiere, True)
            else:
                with tempfile.TemporaryDirectory(prefix="t-studio-") as tmp:
                    checks += render_sample(Path(tmp) / AWKWARD, False)
        except Exception as e:  # noqa: BLE001 - the self-check reports, it does not crash
            checks.append((False, f"пробный рендер не удался: {e}"))
    else:
        checks.append((False, "пробный рендер пропущен: сначала исправьте отмеченное ✗"))
    for ok, text in checks:
        say(f"{'·' if ok is None else '✓' if ok else '✗'} {text}")
    bad = sum(ok is False for ok, _ in checks)
    say("Всё в порядке." if not bad else f"Проблем: {bad}.")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # a Windows console or pipe in cp866/cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m toystudio.doctor", description=__doc__)
    parser.add_argument("--premiere", type=Path, help="папка для набора проверки в Premiere")
    return run(parser.parse_args(argv).premiere)


if __name__ == "__main__":
    raise SystemExit(main())
