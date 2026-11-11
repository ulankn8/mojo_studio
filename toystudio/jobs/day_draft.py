"""The day before the restaurant -> film / express / love story draft + XML for Premiere.

Camera clocks come first: a camera whose clock is off and has no saved correction stops the
draft with DraftError("clocks") so the GUI can ask for one pair of clips (ClockDialog), unless
the user chose to skip, then its clips are spread over the reference camera's day in their
own order.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

import numpy as np

from toystudio.core.ai_plan import (
    AiClip,
    AiPlan,
    AiReview,
    AiShot,
    MusicSection,
    OptionLabel,
    ReviewOption,
    ReviewShot,
    as_part,
)
from toystudio.core.clock import camera_spans, reference_camera, shot_time, suspicious
from toystudio.core.models import US_PER_S, AudioLabel, MediaInfo, MediaRole, MediaRow, MediaStatus
from toystudio.engine.analysis import single_pass
from toystudio.engine.analysis.jitter import is_static, measure, shaky_spans
from toystudio.engine.analysis.single_pass import outputs_for
from toystudio.engine.audio.beats import CONFIDENT, ENV_HZ, Beats, beats_from_envelope
from toystudio.engine.color.grade import LOOK_NAME, balance, chroma, film_chroma, write_cube
from toystudio.engine.edit.day import (
    CLIP_FPS,
    ENERGY_STEP_US,
    FALLBACK_BEAT_US,
    HEAD_US,
    SDE_US,
    DayClip,
    DayDraft,
    MusicTrack,
    _good_start,
    build_clip,
    build_film,
    day_layout,
    love_source,
    music_sections,
    slow_track,
)
from toystudio.engine.edit.love import build_from_plan
from toystudio.engine.edit.overlays import OVERLAYS
from toystudio.engine.edit.overlays import asset as overlay_asset
from toystudio.engine.edit.overlays import place as place_overlays
from toystudio.engine.edit.restaurant import Item
from toystudio.engine.export.premiere_xml import FileRef, write_sequence
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
REVIEW_FRAME_WIDTH = 640  # 640x360 (299 tokens): a squint in a medium shot is a few pixels at 320
AI_FRAME_AT = (0.1, 0.5, 0.9)  # beginning, middle and end: where the best moment is
PLAN_FILE = "love_plan.json"
REVIEW_FILE = "love_review.json"
AI_MUSIC = "T-Studio ИИ"  # file names of the AI's music: "T-Studio ИИ · sde · romantic · <time>"
FIT_MOOD = "fit"  # the mood token of music written to a finished cut's sections
AUDIBLE = 0.05  # of the loudest moment (about -26 dB): where a track's music begins
# the clip each AI piece is written for: (length, tempo); 3 s more than the clip, as a
# margin for the silent lead-in AI music has (the clip ends on the music's own ending)
CLIP_MUSIC: dict[str, tuple[float, int]] = {
    "sde": (SDE_US / 1_000_000 + 3, 92),
    "express": (183.0, 124),
}
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
        made_for = _made_for(m.path)
        hint = CLIP_MUSIC[made_for][1] if made_for else None  # the tempo it was written to
        env = np.load(out.envelope)
        beats = _audible(beats_from_envelope(env, tempo_hint=hint), env)
        fitted = bool(made_for) and m.path.stem.split(" · ")[2:3] == [FIT_MOOD]
        tracks.append(MusicTrack(m.id, info.duration_us, beats, made_for, fitted, _energy(env)))
    return tracks


def _energy(env: np.ndarray) -> tuple[float, ...]:
    """Loudness every ENERGY_STEP_US (in dB, scaled 0..1 over the track's own range)."""
    step = ENV_HZ * ENERGY_STEP_US // US_PER_S
    n = len(env) // step
    if n == 0:
        return ()
    db = 20 * np.log10(np.asarray(env[: n * step]).reshape(n, step).mean(axis=1) + 1e-6)
    lo, hi = float(np.percentile(db, 5)), float(db.max())
    return tuple(float(x) for x in np.clip((db - lo) / max(hi - lo, 1e-6), 0, 1))


def _audible(beats: Beats, env: np.ndarray) -> Beats:
    """Beats from where the music is heard: a silent lead-in (AI music has one) is skipped."""
    loud = np.flatnonzero(env > AUDIBLE * float(env.max())) if len(env) else np.array([])
    if not len(loud) or not beats.beats_us:
        return beats
    start = round(int(loud[0]) * US_PER_S / ENV_HZ) - beats.period_us // 4
    return replace(beats, beats_us=tuple(b for b in beats.beats_us if b >= start))


def _made_for(path: Path) -> str:
    """ "sde" / "express" for music the AI wrote for that clip (see add_ai_music)."""
    parts = path.stem.split(" · ")
    return parts[1] if len(parts) > 2 and parts[0] == AI_MUSIC and parts[1] in CLIP_MUSIC else ""


def add_ai_music(root: Path, audio: bytes, kind: str, mood: str) -> Path:
    """Save the AI's music into the project's music folder and add it to the library."""
    if kind not in CLIP_MUSIC:
        raise ValueError(kind)
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    path = root / "music" / f"{AI_MUSIC} · {kind} · {mood} · {stamp}.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)
    store = ProjectStore.open(root)
    try:
        mid, _ = store.add_media(path, MediaRole.MUSIC, len(audio), True)
        store.set_media_info(mid, probe_media(path))
    finally:
        store.close()
    return path


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


@dataclass(frozen=True)
class _Camera:
    shaky: list[tuple[int, int]]  # hand-shake stretches (µs)
    static: bool  # the camera stood still: such shots get a slow push-in or turn


def _camera(store: ProjectStore, ids: set[int]) -> dict[int, _Camera]:
    """Shake and motion of the clips, measured at the full frame rate on the proxy (cached:
    cache/jitter/{id}.npz); a clip without a proxy is not measured."""
    cache = store.root / "cache"
    rows = {m.id: m for m in store.list_media() if m.id in ids}
    out: dict[int, _Camera] = {}
    for mid, m in rows.items():
        saved = cache / "jitter" / f"{mid}.npz"
        fps = min(m.info.fps if m.info and m.info.fps else 25.0, 50.0)
        data = np.load(saved) if saved.is_file() else None
        if data is not None and "motion" in data:
            values, motion, fps = data["values"], data["motion"], float(data["fps"])
        else:  # not measured yet, or measured before motion was kept
            proxy = outputs_for(cache, mid).proxy
            if not proxy.is_file():
                continue
            try:
                values, motion = measure(proxy, fps)
            except RuntimeError:
                continue
            saved.parent.mkdir(parents=True, exist_ok=True)
            np.savez(saved, values=values, motion=motion, fps=fps)
        out[mid] = _Camera(shaky_spans(values, fps), is_static(motion))
    return out


def _steady(store: ProjectStore, clips: list[DayClip], ids: set[int]) -> list[DayClip]:
    """`clips` with the hand-shake stretches of `ids` among their bad frames, and whether
    their camera stood still."""
    cams = _camera(store, ids)
    return [replace(c, bad=c.bad + tuple(cams[c.media_id].shaky), static=cams[c.media_id].static)
            if c.media_id in cams else c for c in clips]  # fmt: skip


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
    clips: list[DayClip]  # with their shaky stretches among the bad frames


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
    return _Built(draft, titles, media, clips)


def review_inputs(root: Path, *, skip_clocks: bool = False) -> list[ReviewShot]:
    """Every shot of the AI love story as cut, with alternatives for the AI's check: A the shot
    itself, B another clean moment of the same take, C the same length from the nearest take
    of the love story the cut does not use; one frame from the middle of each window."""
    plan = saved_plan(root)
    if plan is None:
        raise DraftError("no_plan")
    store = ProjectStore.open(root)
    try:
        b = _built(store, "sde", skip_clocks, plan)
        by_id = {c.media_id: c for c in b.clips}
        cache = store.root / "cache"
        unused = [
            c for c in love_source(b.clips) if c.media_id not in {s.media_id for s in b.draft.shots}
        ]
        offered: set[int] = set()
        beat = b.draft.beat_us or FALLBACK_BEAT_US
        out: list[ReviewShot] = []
        at = 0
        for n, s in enumerate(b.draft.shots, 1):
            c = by_id[s.media_id]
            a = _option(cache, "A", c, s.src_in, s.src_out - s.src_in, s.speed)
            seen = a.frame.is_file()  # without a frame of its own the shot is kept, not shown
            options = [a]
            src = s.src_out - s.src_in
            far = HEAD_US if s.src_in > c.duration / 2 else c.duration - src  # the other half
            other = _good_start(c, far, src)
            b_opt = (
                _option(cache, "B", c, other, src, s.speed)
                if other is not None and abs(other - s.src_in) >= src
                else None
            )
            if seen and b_opt and b_opt.frame.is_file():
                options.append(b_opt)
            # the nearest unused take not offered to the shot before (a take goes in once:
            # revised_plan keeps a second pick of it as it was)
            near = sorted((u for u in unused if u.media_id not in offered),
                          key=lambda u: abs(u.time - c.time))  # fmt: skip
            offered = set()
            for u in near:
                speed = s.speed if u.can_slow else 1.0
                window = round(s.length * speed)
                start = _good_start(u, (u.duration - window) // 2, window)
                c_opt = _option(cache, "C", u, start, window, speed) if start is not None else None
                if seen and c_opt and c_opt.frame.is_file():
                    options.append(c_opt)
                    offered = {u.media_id}
                    break
            beats = max(1, round(s.length / beat))
            out.append(ReviewShot(n, at, beats, as_part(s.part), s.reason.ru, tuple(options)))
            at += s.length
        return out
    finally:
        store.close()


def _option(
    cache: Path, label: OptionLabel, c: DayClip, src_in: int, src: int, speed: float
) -> ReviewOption:
    """A window and the frame from its middle (cached by clip and moment; the frame is
    missing when the clip has no proxy)."""
    mid = src_in + src // 2
    frame = cache / "thumbs" / f"rv{c.media_id}_{mid // 1000}_{REVIEW_FRAME_WIDTH}.jpg"
    proxy = outputs_for(cache, c.media_id).proxy
    if not frame.is_file() and proxy.is_file():
        with contextlib.suppress(RuntimeError, ValueError, OSError):
            frame_at(proxy, mid, frame, REVIEW_FRAME_WIDTH)
    pct = 100.0 * src_in / c.duration if c.duration else 0.0
    return ReviewOption(label, c.media_id, pct, speed, frame)


def cut_sections(root: Path, kind: str, *, skip_clocks: bool = False) -> list[MusicSection]:
    """The atmosphere of the clip as it is cut now (the AI love story from its saved plan,
    else the rules' clip), for music written to it."""
    plan = saved_plan(root) if kind == "sde" else None
    store = ProjectStore.open(root)
    try:
        b = _built(store, "sde" if kind == "sde" else "express", skip_clocks, plan)
        return music_sections(b.draft)
    finally:
        store.close()


def save_review(root: Path, review: AiReview) -> None:
    """The AI's check of the cut, kept next to its plan (what was swapped and why)."""
    folder = root / "cache" / "ai"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / REVIEW_FILE).write_text(json.dumps(asdict(review), ensure_ascii=False, indent=1,
                                                 default=str), encoding="utf-8")  # fmt: skip


def _overlay_refs(export_dir: Path, items: list[Item]) -> dict[int, FileRef]:
    """The light overlays a layout uses, copied next to the XML so the project travels."""
    refs: dict[int, FileRef] = {}
    for mid in {i.media_id for i in items if i.media_id < 0}:
        src = overlay_asset(mid)
        dst = export_dir / "overlays" / src.name
        if not dst.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        length = next(n for i, n in OVERLAYS.values() if i == mid)
        refs[mid] = FileRef(str(dst), length, 25.0, 1920, 1080, True, 0)
    return refs


def build_day_draft(
    root: Path,
    kind: DayKind,
    *,
    skip_clocks: bool = False,
    ai_plan: AiPlan | None = None,
    effects: bool = True,
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
        items = day_layout(draft) + (place_overlays(draft) if effects else [])
        files = file_refs(store.list_media(), {i.media_id for i in items})
        lead = _lead(b.media)
        xml, name = export_path(root, store, suffix)
        files |= _overlay_refs(xml.parent, items)
        clip = draft.fade  # SDE / express / love story: 25p, dissolves, the look
        write_sequence(xml, name, items, files, CLIP_FPS if clip else lead.fps, lead.width,
                       lead.height, transitions=draft.transitions, fade=draft.fade)  # fmt: skip
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
    effects: bool = True,
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
        lights = place_overlays(b.draft) if effects else []
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
        sources |= {i.media_id: overlay_asset(i.media_id) for i in lights}
        video = render_film(items + lights, sources, durations, out, luts=luts,
                            transitions=b.draft.transitions, fade=b.draft.fade, settings=settings,
                            work=cache / "render", on_progress=on_progress,
                            should_stop=should_stop)  # fmt: skip
        return RenderResult(video, from_proxy)
    finally:
        store.close()
