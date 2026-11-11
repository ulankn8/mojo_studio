"""A finished video of a clip (SDE, express, love story) straight from its layout.

Each shot of V1 is cut from its file, slowed (setpts), coloured with its LUT, framed to the
output size at a constant frame rate, and encoded on its own (a few side by side); shots joined by hard cuts are
concatenated, dissolves are `xfade` between those runs, the film fades in from and out to
black, and the music (and any live sound) is laid under it with a fade at the end. Shots
that dissolve are cut longer by half a dissolve on each side, from the source handles.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from toystudio.core.models import US_PER_S
from toystudio.engine.edit.restaurant import Item, joins
from toystudio.engine.export.premiere_xml import DISSOLVE_US
from toystudio.engine.media.ffmpeg import find_tool, has_filter, popen_flags

FADE_US = 600_000
MUSIC_FADE_US = 2_500_000


class RenderStopped(Exception):
    pass


@dataclass(frozen=True)
class RenderSettings:
    width: int = 1920
    height: int = 1080
    fps: int = 25
    crf: int = 18
    preset: str = "medium"
    vignette: bool = True
    letterbox: bool = False  # 2.39:1 bars

    @staticmethod
    def preview() -> RenderSettings:
        return RenderSettings(1280, 720, 25, 23, "veryfast")


def _run(args: list[str]) -> None:
    proc = subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel",
                           "error", "-y", *args], capture_output=True, check=False,
                          stdin=subprocess.DEVNULL, creationflags=popen_flags())  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-2000:])


def _lut_path(path: Path) -> str:
    """lut3d's file option, escaped for the filter graph (Windows drive colons too)."""
    text = path.as_posix().replace("\\", "/")
    return "'" + text.replace(":", "\\:").replace("'", "\\'") + "'"


def _shot_filter(it: Item, lut: Path | None, st: RenderSettings, frames: int) -> str:
    w, h = st.width, st.height
    parts = [f"setpts=(PTS-STARTPTS)/{it.speed:g}"]
    if lut is not None:
        parts.append(f"lut3d=file={_lut_path(lut)}")
    parts += [f"scale={w}:{h}:force_original_aspect_ratio=increase", f"crop={w}:{h}",
              "setsar=1", f"fps={st.fps}"]  # fmt: skip
    if st.vignette:
        parts.append("vignette=angle=PI/7:eval=init")
    if st.letterbox:
        bar = round((h - w / 2.39) / 2)
        parts.append(f"drawbox=x=0:y=0:w=iw:h={bar}:color=black:t=fill")
        parts.append(f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black:t=fill")
    parts += ["format=yuv420p", f"trim=end_frame={frames}"]
    return ",".join(parts)


def render_film(
    items: Sequence[Item],
    sources: Mapping[int, Path],
    durations: Mapping[int, int],
    out: Path,
    *,
    luts: Mapping[int, Path] | None = None,
    dissolves: Sequence[int] = (),
    fade: bool = True,
    settings: RenderSettings | None = None,
    work: Path | None = None,
    on_progress: Callable[[float], None] = lambda _p: None,
    should_stop: Callable[[], bool] = lambda: False,
) -> Path:
    st = settings or RenderSettings()
    shots = sorted((i for i in items if i.track == "V1"), key=lambda i: i.start)
    if not shots:
        raise ValueError("nothing to render")
    out.parent.mkdir(parents=True, exist_ok=True)
    work = work or out.with_name(out.stem + ".parts")
    work.mkdir(parents=True, exist_ok=True)
    luts = luts if luts and has_filter("lut3d") else {}
    xfade = has_filter("xfade")
    half = DISSOLVE_US // 2
    joined = joins(shots, durations, dissolves, half) if xfade else [False] * (len(shots) - 1)

    def frame(us: int) -> int:
        return round(us * st.fps / US_PER_S)

    hf = frame(half)

    def cut(k: int, it: Item) -> Path:
        if should_stop():
            raise RenderStopped
        before = hf if k and joined[k - 1] else 0
        after = hf if k < len(shots) - 1 and joined[k] else 0
        frames = frame(it.end) - frame(it.start) + before + after
        src_in = it.src_in - round(before * US_PER_S / st.fps * it.speed)
        src_len = round(frames * US_PER_S / st.fps * it.speed) + US_PER_S // st.fps
        piece = work / f"{k:04d}.mp4"
        _run(["-ss", f"{max(src_in, 0) / US_PER_S:.4f}", "-t", f"{src_len / US_PER_S:.4f}",
              "-i", str(sources[it.media_id]), "-an",
              "-vf", _shot_filter(it, luts.get(it.media_id), st, frames),
              "-c:v", "libx264", "-preset", st.preset, "-crf", str(st.crf), "-threads", "2",
              "-r", str(st.fps), str(piece)])  # fmt: skip
        return piece

    workers = max(1, min(4, (os.cpu_count() or 2) // 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:  # shots encode side by side
        futures = [pool.submit(cut, k, it) for k, it in enumerate(shots)]
        try:
            for done, future in enumerate(as_completed(futures), 1):
                future.result()
                on_progress(0.9 * done / len(shots))
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    pieces = [f.result() for f in futures]
    # runs of hard cuts, joined to each other by dissolves
    runs: list[list[Path]] = [[pieces[0]]]
    for k, piece in enumerate(pieces[1:]):
        if joined[k]:
            runs.append([piece])
        else:
            runs[-1].append(piece)
    run_files: list[tuple[Path, int]] = []
    for n, run in enumerate(runs):
        listing = work / f"run{n}.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in run), encoding="utf-8")
        path = work / f"run{n}.mp4"
        _run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(path)])
        run_files.append((path, sum(_frames_of(p) for p in run)))
    total = frame(shots[-1].end) - frame(shots[0].start)
    graph: list[str] = []
    last = "[0:v]"
    length = run_files[0][1]
    for n in range(1, len(run_files)):
        offset = (length - 2 * hf) / st.fps
        graph.append(f"{last}[{n}:v]xfade=transition=fade:duration={2 * hf / st.fps:.4f}"
                     f":offset={offset:.4f}[x{n}]")  # fmt: skip
        last = f"[x{n}]"
        length += run_files[n][1] - 2 * hf
    fades = f"trim=end_frame={total}"
    if fade:
        fd = FADE_US / US_PER_S
        fades += f",fade=t=in:d={fd:.3f},fade=t=out:st={total / st.fps - fd:.3f}:d={fd:.3f}"
    graph.append(f"{last}{fades}[v]")
    inputs: list[str] = []
    for path, _ in run_files:
        inputs += ["-i", str(path)]
    audio_graph, audio_inputs = _audio(items, sources, len(run_files), total / st.fps)
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    _run([*inputs, *audio_inputs, "-filter_complex", ";".join(graph + audio_graph),
          "-map", "[v]", *(["-map", "[a]"] if audio_graph else []),
          "-c:v", "libx264", "-preset", st.preset, "-crf", str(st.crf), "-r", str(st.fps),
          "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(tmp)])  # fmt: skip
    os.replace(tmp, out)
    for p in work.iterdir():
        p.unlink()
    work.rmdir()
    on_progress(1.0)
    return out


def _frames_of(piece: Path) -> int:
    proc = subprocess.run([find_tool("ffprobe"), "-v", "error", "-count_packets",
                           "-select_streams", "v:0", "-show_entries", "stream=nb_read_packets",
                           "-of", "csv=p=0", str(piece)], capture_output=True, check=False,
                          stdin=subprocess.DEVNULL, creationflags=popen_flags())  # fmt: skip
    return int(proc.stdout.decode().strip() or 0)


def _audio(
    items: Sequence[Item], sources: Mapping[int, Path], first: int, seconds: float
) -> tuple[list[str], list[str]]:
    """Music (M) and live sound (A) pieces at their places, mixed, faded out at the end."""
    sound = [i for i in items if i.track in ("A", "M")]
    if not sound:
        return [], []
    inputs: list[str] = []
    graph: list[str] = []
    for n, it in enumerate(sound):
        inputs += ["-ss", f"{it.src_in / US_PER_S:.4f}",
                   "-t", f"{(it.src_out - it.src_in) / US_PER_S:.4f}",
                   "-i", str(sources[it.media_id])]  # fmt: skip
        ms = round(it.start / 1000)
        graph.append(f"[{first + n}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                     f"adelay={ms}|{ms}[s{n}]")  # fmt: skip
    mix = "".join(f"[s{n}]" for n in range(len(sound)))
    fd = MUSIC_FADE_US / US_PER_S
    graph.append(f"{mix}amix=inputs={len(sound)}:normalize=0,atrim=end={seconds:.4f},"
                 f"afade=t=out:st={max(seconds - fd, 0):.3f}:d={fd:.3f}[a]")  # fmt: skip
    return graph, inputs
