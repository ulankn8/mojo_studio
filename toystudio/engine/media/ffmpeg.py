"""Locating ffmpeg/ffprobe and querying their capabilities."""

import os
import re
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Literal

ToolName = Literal["ffmpeg", "ffprobe"]
ENV_DIR = "TOYSTUDIO_FFMPEG_DIR"


def popen_flags() -> int:
    """`creationflags` for every child process: no console window flashes up on Windows."""
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def find_tool(name: ToolName) -> str:
    """Env dir, then ``ffmpeg/`` next to the frozen executable, then PATH."""
    exe = _exe(name)
    candidates: list[Path] = []
    env_dir = os.environ.get(ENV_DIR)
    if env_dir:
        candidates.append(Path(env_dir) / exe)
    candidates.append(Path(sys.executable).parent / "ffmpeg" / exe)
    for c in candidates:
        if c.is_file():
            return str(c)
    from shutil import which

    found = which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name} not found (set {ENV_DIR}, bundle ffmpeg/, or add to PATH)")


@cache
def ffmpeg_major() -> int:
    out = subprocess.run(
        [find_tool("ffmpeg"), "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        creationflags=popen_flags(),
    ).stdout
    m = re.match(r"ffmpeg version n?(\d+)\.", out)
    return int(m.group(1)) if m else 99  # git/master builds: assume new


@cache
def has_filter(name: str) -> bool:
    out = subprocess.run(
        [find_tool("ffmpeg"), "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        creationflags=popen_flags(),
    ).stdout
    return any(len(p) >= 2 and p[1] == name for p in (ln.split() for ln in out.splitlines()))
