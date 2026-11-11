"""Final Cut Pro 7 XML (xmeml v4): Adobe Premiere Pro opens it via File > Import.

Every clip time is written in frames of the sequence. A file is described in full the first
time it appears and referenced by id afterwards, as Premiere expects. Slow motion is the
standard Time Remap filter; dissolves are transition items on V1 (a clip next to one has
start or end -1 and its in/out reach into the handle, as FCP7 writes them).
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from toystudio.core.models import US_PER_S
from toystudio.engine.edit.restaurant import Item, joins

_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
SAMPLE_RATE = 48_000
DISSOLVE_US = 480_000  # 12 frames at 25 fps


@dataclass(frozen=True)
class FileRef:
    path: str  # as stored in the project: a Windows or a POSIX path
    duration_us: int
    fps: float
    width: int
    height: int
    has_video: bool
    audio_channels: int


def path_url(path: str) -> str:
    """file://localhost/C%3A/... for Windows paths, file://localhost/... for POSIX ones."""
    if _WINDOWS_PATH.match(path):
        return "file://localhost/" + quote(path.replace("\\", "/"), safe="/")
    return "file://localhost" + quote(path, safe="/")


def timebase(fps: float) -> tuple[int, bool]:
    """(timebase, ntsc): 50 -> (50, False), 29.97 -> (30, True)."""
    base = round(fps) or 25
    return base, abs(fps - base) > 0.01


class _Frames:
    def __init__(self, fps: float) -> None:
        self.base, self.ntsc = timebase(fps)
        self.rate = self.base * 1000 / 1001 if self.ntsc else float(self.base)

    def __call__(self, us: int) -> int:
        return round(us * self.rate / US_PER_S)

    def element(self) -> ET.Element:
        rate = ET.Element("rate")
        _text(rate, "timebase", self.base)
        _text(rate, "ntsc", "TRUE" if self.ntsc else "FALSE")
        return rate


def _text(parent: ET.Element, tag: str, value: object) -> ET.Element:
    el = ET.SubElement(parent, tag)
    el.text = str(value)
    return el


def sequence_xml(
    name: str,
    items: Sequence[Item],
    files: dict[int, FileRef],
    fps: float,
    width: int,
    height: int,
    *,
    transitions: Mapping[int, str] | None = None,
    fade: bool = False,
) -> str:
    """`transitions`: V1 cuts (position in us -> "fade" / "white" / "whip"), all written as
    Cross Dissolves of their length; `fade`: from black at the start and to black at the
    end. A transition without enough media on both sides is left a cut."""
    fr = _Frames(fps)
    root = ET.Element("xmeml", version="4")
    seq = ET.SubElement(root, "sequence", id="sequence-1")
    _text(seq, "name", name)
    _text(seq, "duration", fr(max((i.end for i in items), default=0)))
    seq.append(fr.element())
    media = ET.SubElement(seq, "media")
    video = ET.SubElement(media, "video")
    fmt = ET.SubElement(ET.SubElement(video, "format"), "samplecharacteristics")
    fmt.append(fr.element())
    _text(fmt, "width", width)
    _text(fmt, "height", height)
    _text(fmt, "pixelaspectratio", "square")
    _text(fmt, "fielddominance", "none")
    audio = ET.SubElement(media, "audio")
    _text(audio, "numOutputChannels", 2)
    afmt = ET.SubElement(ET.SubElement(audio, "format"), "samplecharacteristics")
    _text(afmt, "depth", 16)
    _text(afmt, "samplerate", SAMPLE_RATE)

    described: set[int] = set()
    count = [0]

    def clipitem(
        track: ET.Element,
        it: Item,
        kind: str,
        channel: int = 1,
        before: int | None = None,
        after: int | None = None,
    ) -> None:
        """`before` / `after`: frames of a transition reaching into this clip's handles."""
        f = files[it.media_id]
        count[0] += 1
        ci = ET.SubElement(track, "clipitem", id=f"clipitem-{count[0]}")
        _text(ci, "name", Path(f.path.replace("\\", "/")).name)
        _text(ci, "enabled", "TRUE")
        _text(ci, "duration", fr(f.duration_us))
        ci.append(fr.element())
        start, end = fr(it.start), fr(it.end)
        src_in = fr(it.src_in)
        src_out = src_in + round((end - start) * it.speed)
        if before is not None:
            src_in -= round(before * it.speed)
        if after is not None:
            src_out += round(after * it.speed)
        _text(ci, "start", -1 if before is not None else start)
        _text(ci, "end", -1 if after is not None else end)
        _text(ci, "in", src_in)
        _text(ci, "out", src_out)
        ci.append(_file(it.media_id, f, fr, full=it.media_id not in described))
        described.add(it.media_id)
        if kind == "video" and it.blend:  # a light overlay: screen mode, its own opacity
            _text(ci, "compositemode", it.blend)
            ci.append(_opacity(it.opacity))
        if kind == "video" and it.speed != 1.0:
            ci.append(_time_remap(it.speed))
        if kind == "video" and it.move:
            ci.append(_motion(it.move, src_in, src_out))
        if kind == "audio":
            src = ET.SubElement(ci, "sourcetrack")
            _text(src, "mediatype", "audio")
            _text(src, "trackindex", channel)

    def transition(track: ET.Element, start: int, end: int, alignment: str) -> None:
        tr = ET.SubElement(track, "transitionitem")
        _text(tr, "start", start)
        _text(tr, "end", end)
        _text(tr, "alignment", alignment)
        tr.append(fr.element())
        effect = ET.SubElement(tr, "effect")
        for tag, value in (("name", "Cross Dissolve"), ("effectid", "Cross Dissolve"),
                           ("effectcategory", "Dissolve"), ("effecttype", "transition"),
                           ("mediatype", "video"), ("wipecode", 0), ("wipeaccuracy", 100),
                           ("startratio", 0), ("endratio", 1), ("reverse", "FALSE")):  # fmt: skip
            _text(effect, tag, value)

    def main_track(track: ET.Element, clips: list[Item]) -> None:
        """V1 with its dissolves: clip, transition, clip ..."""
        durations = {i: f.duration_us for i, f in files.items()}
        halves = [fr(h) for h in joins(clips, durations, transitions or {})]
        edge = fr(DISSOLVE_US) // 2
        fade_in = fade and bool(clips) and clips[0].start == 0
        fade_out = fade and bool(clips)
        if fade_in:
            transition(track, 0, 2 * edge, "start-black")
        for k, it in enumerate(clips):
            last = k == len(clips) - 1
            before = halves[k - 1] if k and halves[k - 1] else (0 if k == 0 and fade_in else None)
            after = halves[k] if not last and halves[k] else (0 if last and fade_out else None)
            clipitem(track, it, "video", before=before, after=after)
            if not last and halves[k]:
                at = fr(clips[k + 1].start)
                transition(track, at - halves[k], at + halves[k], "center")
        if fade_out:
            end = fr(clips[-1].end)
            transition(track, end - 2 * edge, end, "end-black")

    main_track(ET.SubElement(video, "track"), sorted((i for i in items if i.track == "V1"),
                                                     key=lambda i: i.start))  # fmt: skip
    inserts = ET.SubElement(video, "track")
    for it in items:
        if it.track == "V2":
            clipitem(inserts, it, "video")
    for group in ("A", "M"):  # live / reference sound, then music on its own tracks
        sound = [i for i in items if i.track == group]
        if not sound and group == "M":
            continue
        channels = max((min(files[i.media_id].audio_channels, 2) for i in sound), default=1)
        for ch in range(1, max(channels, 1) + 1):  # stereo: left, then right
            track = ET.SubElement(audio, "track")
            for it in sound:
                if files[it.media_id].audio_channels >= ch:
                    clipitem(track, it, "audio", ch)
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n"


MOVE_ZOOM = 1.08  # a push-in / pull-out goes between 100 % and this
MOVE_TURN_DEG = 1.5  # a turn goes from -this to +this, zoomed so no corner shows


def move_keys(move: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """((scale at the start, at the end), (rotation at the start, at the end)) of a move."""
    zoom = MOVE_ZOOM * 100
    return {
        "in": ((100.0, zoom), (0.0, 0.0)),
        "out": ((zoom, 100.0), (0.0, 0.0)),
        "turn": ((zoom, zoom), (-MOVE_TURN_DEG, MOVE_TURN_DEG)),
    }.get(move, ((100.0, 100.0), (0.0, 0.0)))


def _motion(move: str, first: int, last: int) -> ET.Element:
    """Basic Motion with scale / rotation keyframes at the clip's in and out (source frames)."""
    (s0, s1), (r0, r1) = move_keys(move)
    flt = ET.Element("filter")
    effect = ET.SubElement(flt, "effect")
    for tag, value in (("name", "Basic Motion"), ("effectid", "basic"),
                       ("effectcategory", "motion"), ("effecttype", "motion"),
                       ("mediatype", "video")):  # fmt: skip
        _text(effect, tag, value)
    for pid, name, lo, hi, a, b in (("scale", "Scale", 0, 1000, s0, s1),
                                    ("rotation", "Rotation", -8640, 8640, r0, r1)):  # fmt: skip
        par = ET.SubElement(effect, "parameter")
        _text(par, "parameterid", pid)
        _text(par, "name", name)
        _text(par, "valuemin", lo)
        _text(par, "valuemax", hi)
        _text(par, "value", f"{a:g}")
        for when, at in ((first, a), (last, b)):
            key = ET.SubElement(par, "keyframe")
            _text(key, "when", when)
            _text(key, "value", f"{at:g}")
    return flt


def _opacity(opacity: float) -> ET.Element:
    flt = ET.Element("filter")
    effect = ET.SubElement(flt, "effect")
    for tag, value in (("name", "Opacity"), ("effectid", "opacity"),
                       ("effectcategory", "motion"), ("effecttype", "motion"),
                       ("mediatype", "video")):  # fmt: skip
        _text(effect, tag, value)
    par = ET.SubElement(effect, "parameter")
    _text(par, "parameterid", "opacity")
    _text(par, "name", "Opacity")
    _text(par, "valuemin", 0)
    _text(par, "valuemax", 100)
    _text(par, "value", f"{opacity * 100:g}")
    return flt


def _time_remap(speed: float) -> ET.Element:
    """Constant speed as FCP7 / Premiere write it (Clip > Speed/Duration)."""
    flt = ET.Element("filter")
    effect = ET.SubElement(flt, "effect")
    for tag, value in (("name", "Time Remap"), ("effectid", "timeremap"),
                       ("effectcategory", "motion"), ("effecttype", "motion"),
                       ("mediatype", "video")):  # fmt: skip
        _text(effect, tag, value)
    for pid, setting in (("variablespeed", 0), ("speed", f"{speed * 100:g}"),
                         ("reverse", "FALSE"), ("frameblending", "FALSE")):  # fmt: skip
        par = ET.SubElement(effect, "parameter")
        _text(par, "parameterid", pid)
        _text(par, "name", pid)
        _text(par, "value", setting)
    return flt


def _file(media_id: int, f: FileRef, fr: _Frames, *, full: bool) -> ET.Element:
    el = ET.Element("file", id=f"file-{media_id}")
    if not full:
        return el
    _text(el, "name", Path(f.path.replace("\\", "/")).name)
    _text(el, "pathurl", path_url(f.path))
    el.append(fr.element())
    _text(el, "duration", fr(f.duration_us))
    media = ET.SubElement(el, "media")
    if f.has_video:
        sc = ET.SubElement(ET.SubElement(media, "video"), "samplecharacteristics")
        sc.append(_Frames(f.fps).element())
        _text(sc, "width", f.width)
        _text(sc, "height", f.height)
    if f.audio_channels:
        a = ET.SubElement(media, "audio")
        sc = ET.SubElement(a, "samplecharacteristics")
        _text(sc, "depth", 16)
        _text(sc, "samplerate", SAMPLE_RATE)
        _text(a, "channelcount", f.audio_channels)
    return el


def write_sequence(
    path: Path,
    name: str,
    items: Sequence[Item],
    files: dict[int, FileRef],
    fps: float,
    width: int,
    height: int,
    *,
    transitions: Mapping[int, str] | None = None,
    fade: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = sequence_xml(name, items, files, fps, width, height, transitions=transitions, fade=fade)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
