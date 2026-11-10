"""Restaurant draft: analysis signals -> auto-edit -> Cut in project.db + XML for Premiere.

Takes a fraction of a second, so the GUI calls it directly after the analysis is done.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from toystudio.core.models import AudioLabel, MediaRole, MediaRow, MediaStatus, ProjectFormat
from toystudio.engine.edit.restaurant import Clip, DraftStats, RefAudio, build, layered
from toystudio.engine.export.premiere_xml import FileRef, write_sequence
from toystudio.engine.media.probe import audio_channels
from toystudio.storage.project_store import ProjectStore

EXPORT_DIR = "export"
CUT_TITLE = "Ресторан · черновик"
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DraftError(Exception):
    """`code`: no_synced_cameras | nothing_to_keep."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DraftResult:
    xml_path: Path
    cut_id: str
    stats: DraftStats


def _synced(media: list[MediaRow], role: MediaRole) -> list[MediaRow]:
    return [m for m in media if m.role is role and m.sync_offset_us is not None and m.info]


def build_draft(root: Path) -> DraftResult:
    store = ProjectStore.open(root)
    try:
        media = [m for m in store.list_media() if m.status is MediaStatus.OK]
        cams = _synced(media, MediaRole.MAIN) + _synced(media, MediaRole.CUTAWAY)
        if not cams:
            raise DraftError("no_synced_cameras")
        clips = [
            Clip(
                m.id,
                m.role,
                m.sync_offset_us or 0,
                m.info.duration_us if m.info else 0,
                tuple((q.start_us, q.end_us) for q in store.quality_intervals(m.id)),
            )
            for m in cams
        ]
        # the clean recording is the reference; without it the main camera's own sound
        ref_rows = _synced(media, MediaRole.SPEECH) or _synced(media, MediaRole.MAIN)
        refs, speech, music = [], [], []
        for m in ref_rows:
            off = m.sync_offset_us or 0
            refs.append(RefAudio(m.id, off, m.info.duration_us if m.info else 0))
            speech += [(off + r.start_us, off + r.end_us) for r in store.speech_regions(m.id)]
            music += [(off + e.start_us, off + e.end_us) for e in store.audio_events(m.id)
                      if e.label is AudioLabel.MUSIC]  # fmt: skip
        draft = build(clips, refs, speech, music)
        if not draft.shots:
            raise DraftError("nothing_to_keep")
        cut_id = store.create_cut(CUT_TITLE, "16:9", draft.stats.duration_us)
        store.save_timeline(cut_id, draft.timeline)
        used = {c.media_id for c in clips} | {r.media_id for r in refs}
        files = {
            m.id: FileRef(
                str(m.path),
                m.info.duration_us,
                m.info.fps,
                m.info.width,
                m.info.height,
                m.info.has_video,
                audio_channels(m.path) if m.info.has_audio else 0,
            )
            for m in media
            if m.id in used and m.info is not None
        }
        lead = (_synced(media, MediaRole.MAIN) or cams)[0].info
        assert lead is not None
        suffix = "ресторан" if store.format is ProjectFormat.WEDDING_FULL else "черновик"
        name = _INVALID.sub("", store.name).strip() or "Проект"
        xml = root / EXPORT_DIR / f"{name} · {suffix}.xml"
        write_sequence(xml, f"{name} · {suffix}", layered(draft, clips), files,
                       lead.fps, lead.width, lead.height)  # fmt: skip
        return DraftResult(xml, cut_id, draft.stats)
    finally:
        store.close()
