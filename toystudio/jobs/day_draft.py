"""The day before the restaurant -> film / express / love story draft + XML for Premiere.

Camera clocks come first: a camera whose clock is off and has no saved correction stops the
draft with DraftError("clocks") so the GUI can ask for one pair of clips (ClockDialog), unless
the user chose to skip, then its clips are spread over the reference camera's day in their
own order.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

import numpy as np

from toystudio.core.ai_plan import AiClip, AiPlan, AiShot
from toystudio.core.clock import camera_spans, reference_camera, shot_time, suspicious
from toystudio.core.models import AudioLabel, MediaInfo, MediaRole, MediaRow, MediaStatus
from toystudio.engine.analysis import single_pass
from toystudio.engine.analysis.jitter import jitter, shaky_spans
from toystudio.engine.analysis.single_pass import outputs_for
from toystudio.engine.audio.beats import CONFIDENT, beats_from_envelope
from toystudio.engine.color.grade import LOOK_NAME, balance, chroma, film_chroma, write_cube
from toystudio.engine.edit.day import (
    CLIP_FPS,
    FALLBACK_BEAT_US,
    SDE_US,
    DayClip,
    DayDraft,
    MusicTrack,
    build_clip,
    build_film,
    day_layout,
    love_source,
    slow_track,
)
from toystudio.engine.edit.love import build_from_plan
from toystudio.engine.export.premiere_xml import write_sequence
from toystudio.engine.media.probe import check_integrity, probe_media
from toystudio.engine.media.thumbs import average_hash, frame_at, small_rgb
from toystudio.engine.render.film import RenderSettings, render_film
from toystudio.jobs.restaurant_draft import DraftError, DraftResult, export_path, file_refs
from toystudio.storage.project_store import ProjectStore

DayKind = Literal["film", "express", "sde"]
_TITLES: dict[str, tuple[str, str]] = {  # kind -> (cut title, file suffix)
    "film": ("До ресторана · черновик", "до ресторана"),
    "express": ("Экспресс · черновик", "экспресс"),
    "sde": ("Love Story · черновик", "love story"),
    "sde_ai": ("Love Story · ИИ", "love story ИИ"),
}
AI_FRAME_WIDTH = 320  # 320x180: 84 tokens a frame
AI_FRAME_AT = (0.1, 0.5, 0.9)  # beginning, middle and end: where the best moment is
PLAN_FILE = "love_plan.json"
AI_FLAG_SHARE = 0.3  # a clip is «трясётся» when this much of it shakes
_FLAG_TEXT = {"shaky": "трясётся", "blurry": "размыто", "dark": "темно"}


@dataclass(frozen=True)
class LoveInputs:
    """What the AI director gets: the love story's clips with frames, and the music grid."""

    clips: list[AiClip]
    tempo: float
    beat_us: int
    target_us: int


@dataclass(frozen=True)
class Clocks:
    """What the clock dialog shows: the reference camera and the cameras to match to it."""

    reference: str | None
    to_fix: list[str]
    clips: dict[str, list[MediaRow]]  # camera -> its clips in shooting order


def _pre(store: ProjectStore) -> list[MediaRow]:
    return [m for m in store.list_media()
            if m.role is MediaRole.PRE and m.status is MediaStatus.OK and m.info]  # fmt: skip


def clocks(store: ProjectStore) -> Clocks:
    media = _pre(store)
    spans = camera_spans(
        (m.camera, shot_time(m.info.creation_time if m.info else None),
         m.info.duration_us if m.info else 0)
        for m in media
    )  # fmt: skip
    ref = reference_camera(spans)
    to_fix = suspicious({m.camera for m in media}, spans, ref, store.camera_clock())
    by_cam: dict[str, list[MediaRow]] = {}
    for m in sorted(media, key=lambda m: (shot_time(m.info.creation_time if m.info else None)
                                          or 0, m.path.name)):  # fmt: skip
        by_cam.setdefault(m.camera, []).append(m)
    return Clocks(ref, to_fix, by_cam)


def _times(store: ProjectStore, media: list[MediaRow], skip_clocks: bool) -> dict[int, int]:
    """Clip start on the common clock: own time + camera correction; unmatched cameras that the
    user skipped are spread over the reference camera's day in their own order."""
    c = clocks(store)
    if c.to_fix and not skip_clocks:
        raise DraftError("clocks")
    fixed = store.camera_clock()
    spans = camera_spans((m.camera, shot_time(m.info.creation_time if m.info else None),
                          m.info.duration_us if m.info else 0) for m in media)  # fmt: skip
    ref_span = spans.get(c.reference or "")
    out: dict[int, int] = {}
    for cam, rows in c.clips.items():
        for i, m in enumerate(rows):
            own = shot_time(m.info.creation_time if m.info else None)
            if cam in c.to_fix and ref_span is not None:  # spread in its own order
                frac = (i + 0.5) / len(rows)
                out[m.id] = ref_span.start + round(frac * (ref_span.end - ref_span.start))
            else:
                out[m.id] = (own if own is not None else i) + fixed.get(cam, 0)
    return out


def _music(store: ProjectStore) -> list[MusicTrack]:
    """Tempo and beats of every track of the music library (decoded once, cached)."""
    tracks: list[MusicTrack] = []
    for m in store.list_media():
        if m.role is not MediaRole.MUSIC or m.status is not MediaStatus.OK:
            continue
        info = m.info
        if info is None:
            if not m.path.is_file() or not check_integrity(m.path):
                continue
            info = probe_media(m.path)
            store.set_media_info(m.id, info)
        out = outputs_for(store.root / "cache", m.id)
        if not out.envelope.is_file():
            single_pass.run_audio(m.path, info, out, on_progress=lambda _s: None,
                                  should_stop=lambda: False)  # fmt: skip
        beats = beats_from_envelope(np.load(out.envelope))
        tracks.append(MusicTrack(m.id, info.duration_us, beats))
    return tracks


def _day_clips(store: ProjectStore, media: list[MediaRow], skip_clocks: bool) -> list[DayClip]:
    times = _times(store, media, skip_clocks)
    return [
        DayClip(
            m.id,
            m.camera,
            times[m.id],
            m.info.duration_us if m.info else 0,
            bool(m.info and m.info.has_audio),
            tuple((r.start_us, r.end_us) for r in store.speech_regions(m.id)),
            tuple(
                (e.start_us, e.end_us)
                for e in store.audio_events(m.id)
                if e.label is AudioLabel.MUSIC
            ),
            tuple((q.start_us, q.end_us) for q in store.quality_intervals(m.id)),
            m.info.fps if m.info else 0.0,
        )
        for m in media
    ]


def _share(spans: tuple[tuple[int, int], ...], duration: int) -> float:
    return sum(b - a for a, b in spans) / duration if duration else 0.0


def _flags(store: ProjectStore, m: MediaRow) -> tuple[str, ...]:
    dur = m.info.duration_us if m.info else 0
    share: dict[str, int] = {}
    for q in store.quality_intervals(m.id):
        share[q.label.value] = share.get(q.label.value, 0) + q.end_us - q.start_us
    return tuple(_FLAG_TEXT[k] for k, v in sorted(share.items())
                 if k in _FLAG_TEXT and dur and v / dur >= AI_FLAG_SHARE)  # fmt: skip


def _shaky(store: ProjectStore, ids: set[int]) -> dict[int, list[tuple[int, int]]]:
    """Hand-shake stretches of the clips, measured at the full frame rate on the proxy
    (cached: cache/jitter/{id}.npz); a clip without a proxy is not measured."""
    cache = store.root / "cache"
    rows = {m.id: m for m in store.list_media() if m.id in ids}
    out: dict[int, list[tuple[int, int]]] = {}
    for mid, m in rows.items():
        saved = cache / "jitter" / f"{mid}.npz"
        fps = min(m.info.fps if m.info and m.info.fps else 25.0, 50.0)
        if saved.is_file():
            data = np.load(saved)
            values, fps = data["values"], float(data["fps"])
        else:
            proxy = outputs_for(cache, mid).proxy
            if not proxy.is_file():
                continue
            try:
                values = jitter(proxy, fps)
            except RuntimeError:
                continue
            saved.parent.mkdir(parents=True, exist_ok=True)
            np.savez(saved, values=values, fps=fps)
        out[mid] = shaky_spans(values, fps)
    return out


def _steady(store: ProjectStore, clips: list[DayClip], ids: set[int]) -> list[DayClip]:
    """`clips` with the hand-shake stretches of `ids` among their bad frames."""
    spans = _shaky(store, ids)
    return [replace(c, bad=c.bad + tuple(spans[c.media_id])) if spans.get(c.media_id) else c
            for c in clips]  # fmt: skip


def _ai_frame(cache: Path, media_id: int, at: float) -> Path:
    return cache / "thumbs" / f"ai{media_id}_{round(at * 100)}.jpg"


def looks(cache: Path, ids: set[int]) -> dict[int, int]:
    """Average hashes of the clips' middle frames (those the AI director saw), for spotting
    look-alike shots side by side; a clip without a frame is simply not compared."""
    out: dict[int, int] = {}
    for i in ids:
        frame = _ai_frame(cache, i, 0.5)
        if frame.is_file():
            with contextlib.suppress(RuntimeError, OSError):
                out[i] = average_hash(frame)
    return out


def love_inputs(root: Path, *, skip_clocks: bool = False) -> LoveInputs:
    """The love story's clips with two small frames each (cached) and the slow track's beat."""
    store = ProjectStore.open(root)
    try:
        media = _pre(store)
        if not media:
            raise DraftError("nothing_to_keep")
        clips = _day_clips(store, media, skip_clocks)
        clips = _steady(store, clips, {c.media_id for c in love_source(clips)})
        rows = {m.id: m for m in media}
        cache = store.root / "cache"
        ai_clips: list[AiClip] = []
        for c in love_source(clips):
            proxy = outputs_for(cache, c.media_id).proxy
            frames: list[Path] = []
            for at in AI_FRAME_AT:
                out = _ai_frame(cache, c.media_id, at)
                if not out.is_file() and proxy.is_file():
                    with contextlib.suppress(RuntimeError, ValueError, OSError):
                        frame_at(proxy, round(c.duration * at), out, AI_FRAME_WIDTH)
                if out.is_file():
                    frames.append(out)
            if frames:
                info = rows[c.media_id].info
                flags = _flags(store, rows[c.media_id])
                if _share(c.bad, c.duration) >= AI_FLAG_SHARE and "трясётся" not in flags:
                    flags = (*flags, "трясётся")
                ai_clips.append(AiClip(c.media_id, c.duration, flags, tuple(frames),
                                       info.fps if info else 0.0))  # fmt: skip
        track = slow_track(_music(store))
        beat = track.beat_us if track else FALLBACK_BEAT_US
        tempo = track.beats.tempo if track and track.beats.confidence >= CONFIDENT else 0.0
        return LoveInputs(ai_clips, tempo, beat, SDE_US)
    finally:
        store.close()


@dataclass(frozen=True)
class _Built:
    draft: DayDraft
    titles: tuple[str, str]
    media: list[MediaRow]


def _built(store: ProjectStore, kind: DayKind, skip_clocks: bool, ai_plan: AiPlan | None) -> _Built:
    media = _pre(store)
    if not media:
        raise DraftError("nothing_to_keep")
    clips = _day_clips(store, media, skip_clocks)
    tracks = _music(store)
    if ai_plan is not None or kind == "sde":
        clips = _steady(store, clips, {c.media_id for c in love_source(clips)})
    if ai_plan is not None:
        seen = looks(store.root / "cache", {s.clip_id for s in ai_plan.shots})
        draft = build_from_plan(ai_plan, clips, slow_track(tracks), SDE_US, seen)
        titles = _TITLES["sde_ai"]
    elif kind == "film":
        draft, titles = build_film(clips, tracks), _TITLES[kind]
    else:
        draft, titles = build_clip(clips, tracks, kind), _TITLES[kind]
        if kind == "express":  # too many clips to measure all: measure the chosen, rebuild
            clips = _steady(store, clips, {s.media_id for s in draft.shots})
            draft = build_clip(clips, tracks, kind)
    if not draft.shots:
        raise DraftError("nothing_to_keep")
    return _Built(draft, titles, media)


def build_day_draft(
    root: Path, kind: DayKind, *, skip_clocks: bool = False, ai_plan: AiPlan | None = None
) -> DraftResult:
    """A draft for Premiere; with `ai_plan` the love story follows the AI director's plan.
    Clips (SDE, express, love story) also get the look LUT next to the XML."""
    store = ProjectStore.open(root)
    try:
        b = _built(store, kind, skip_clocks, ai_plan)
        draft, (title, suffix) = b.draft, b.titles
        if ai_plan is not None:
            plan_file = store.root / "cache" / "ai" / PLAN_FILE
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(json.dumps(asdict(ai_plan), ensure_ascii=False, indent=1,
                                            default=str), encoding="utf-8")  # fmt: skip
        cut_id = store.create_cut(title, "16:9", draft.stats.duration_us)
        store.save_timeline(cut_id, draft.timeline)
        items = day_layout(draft)
        files = file_refs(store.list_media(), {i.media_id for i in items})
        lead = _lead(b.media)
        xml, name = export_path(root, store, suffix)
        clip = draft.fade  # SDE / express / love story: 25p, dissolves, the look
        write_sequence(xml, name, items, files, CLIP_FPS if clip else lead.fps, lead.width,
                       lead.height, dissolves=draft.dissolves, fade=draft.fade)  # fmt: skip
        if clip:
            write_cube(xml.parent / f"{LOOK_NAME}.cube")
        return DraftResult(xml, cut_id, draft.stats)
    finally:
        store.close()


def _length(m: MediaRow) -> int:
    return m.info.duration_us if m.info else 0


def _lead(media: list[MediaRow]) -> MediaInfo:
    lead = max((m.info for m in media if m.info and m.info.has_audio),
               key=lambda i: i.duration_us, default=media[0].info)  # fmt: skip
    assert lead is not None
    return lead


def saved_plan(root: Path) -> AiPlan | None:
    """The AI director's last plan of this project (rebuilding it costs nothing)."""
    path = root / "cache" / "ai" / PLAN_FILE
    if not path.is_file():
        return None
    obj = json.loads(path.read_text("utf-8"))
    shots = tuple(AiShot(**s) for s in obj.pop("shots"))
    return AiPlan(shots=shots, **obj)


def clip_luts(store: ProjectStore, clips: dict[int, int]) -> dict[int, Path]:
    """Per clip (media id -> duration) a LUT: its balance towards the film's colour, then the
    look. Frames are the AI director's (cached), made from the proxy when missing."""
    cache = store.root / "cache"
    frames: dict[int, list[np.ndarray]] = {}
    for mid, duration in clips.items():
        stills = []
        for at in AI_FRAME_AT:
            path = _ai_frame(cache, mid, at)
            proxy = outputs_for(cache, mid).proxy
            with contextlib.suppress(RuntimeError, ValueError, OSError):
                if not path.is_file() and proxy.is_file():
                    frame_at(proxy, round(duration * at), path, AI_FRAME_WIDTH)
                if path.is_file():
                    stills.append(small_rgb(path))
        frames[mid] = stills
    target = film_chroma([chroma(f) for f in frames.values()])
    return {mid: write_cube(cache / "grade" / f"{mid}.cube", balance(f, target))
            for mid, f in frames.items()}  # fmt: skip


@dataclass(frozen=True)
class RenderResult:
    video: Path
    from_proxy: bool  # the original files were not reachable (a drive unplugged)


def render_draft(
    root: Path,
    kind: DayKind | Literal["sde_ai"],
    *,
    preview: bool = False,
    skip_clocks: bool = True,
    on_progress: Callable[[float], None] = lambda _p: None,
    should_stop: Callable[[], bool] = lambda: False,
) -> RenderResult:
    """The finished video of a clip, coloured, from the same draft as its XML (an AI love
    story from the saved plan, so no new request). `preview`: small and fast."""
    store = ProjectStore.open(root)
    try:
        plan = saved_plan(root) if kind == "sde_ai" else None
        if kind == "sde_ai" and plan is None:
            raise DraftError("no_plan")
        b = _built(store, "sde" if kind == "sde_ai" else kind, skip_clocks, plan)
        items = day_layout(b.draft)
        rows = {m.id: m for m in store.list_media()}
        used = {i.media_id for i in items}
        cache = store.root / "cache"
        sources: dict[int, Path] = {}
        from_proxy = False
        for mid in used:
            proxy = outputs_for(cache, mid).proxy
            original = rows[mid].path
            use_proxy = rows[mid].role is not MediaRole.MUSIC and (
                preview or not original.is_file()
            )
            if use_proxy and proxy.is_file():
                sources[mid] = proxy
                from_proxy = from_proxy or not preview
            elif original.is_file():
                sources[mid] = original
            else:
                raise DraftError("missing_files")
        durations = {mid: _length(rows[mid]) for mid in used}
        shots = {i.media_id for i in items if i.track == "V1"}
        luts = clip_luts(store, {mid: durations[mid] for mid in shots})
        xml, _ = export_path(root, store, b.titles[1])
        out = xml.with_suffix(".mp4") if not preview else xml.with_name(xml.stem + " · превью.mp4")
        settings = RenderSettings.preview() if preview else RenderSettings()
        video = render_film(items, sources, durations, out, luts=luts,
                            dissolves=b.draft.dissolves, fade=b.draft.fade, settings=settings,
                            work=cache / "render", on_progress=on_progress,
                            should_stop=should_stop)  # fmt: skip
        return RenderResult(video, from_proxy)
    finally:
        store.close()
