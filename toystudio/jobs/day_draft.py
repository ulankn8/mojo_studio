"""The day before the restaurant -> film / express / love story draft + XML for Premiere.

Camera clocks come first: a camera whose clock is off and has no saved correction stops the
draft with DraftError("clocks") so the GUI can ask for one pair of clips (ClockDialog), unless
the user chose to skip, then its clips are spread over the reference camera's day in their
own order.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from toystudio.core.clock import camera_spans, reference_camera, shot_time, suspicious
from toystudio.core.models import AudioLabel, MediaRole, MediaRow, MediaStatus
from toystudio.engine.analysis import single_pass
from toystudio.engine.analysis.single_pass import outputs_for
from toystudio.engine.audio.beats import beats_from_envelope
from toystudio.engine.edit.day import DayClip, MusicTrack, build_clip, build_film, day_layout
from toystudio.engine.export.premiere_xml import write_sequence
from toystudio.engine.media.probe import check_integrity, probe_media
from toystudio.jobs.restaurant_draft import DraftError, DraftResult, export_path, file_refs
from toystudio.storage.project_store import ProjectStore

DayKind = Literal["film", "express", "sde"]
_TITLES: dict[str, tuple[str, str]] = {  # kind -> (cut title, file suffix)
    "film": ("До ресторана · черновик", "до ресторана"),
    "express": ("Экспресс · черновик", "экспресс"),
    "sde": ("Love Story · черновик", "love story"),
}


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


def build_day_draft(root: Path, kind: DayKind, *, skip_clocks: bool = False) -> DraftResult:
    store = ProjectStore.open(root)
    try:
        media = _pre(store)
        if not media:
            raise DraftError("nothing_to_keep")
        times = _times(store, media, skip_clocks)
        clips = [
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
            )
            for m in media
        ]
        tracks = _music(store)
        draft = build_film(clips, tracks) if kind == "film" else build_clip(clips, tracks, kind)
        if not draft.shots:
            raise DraftError("nothing_to_keep")
        title, suffix = _TITLES[kind]
        cut_id = store.create_cut(title, "16:9", draft.stats.duration_us)
        store.save_timeline(cut_id, draft.timeline)
        items = day_layout(draft)
        all_media = store.list_media()
        files = file_refs(all_media, {i.media_id for i in items})
        lead = max((m.info for m in media if m.info and m.info.has_audio),
                   key=lambda i: i.duration_us, default=media[0].info)  # fmt: skip
        assert lead is not None
        xml, name = export_path(root, store, suffix)
        write_sequence(xml, name, items, files, lead.fps, lead.width, lead.height)
        return DraftResult(xml, cut_id, draft.stats)
    finally:
        store.close()
