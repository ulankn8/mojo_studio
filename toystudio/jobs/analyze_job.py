"""Worker job `analyze`: per-file stages with checkpoints in project.db, then camera sync."""

from __future__ import annotations

import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from toystudio.core.eta import DEFAULT_X_RT, SPEECH_X_RT, profile_key, project_remaining_s
from toystudio.core.models import (
    STAGE_ORDER,
    US_PER_S,
    MediaInfo,
    MediaKind,
    MediaRole,
    MediaRow,
    MediaStatus,
    StageName,
    StageState,
)
from toystudio.engine.analysis import single_pass
from toystudio.engine.analysis.quality import bad_intervals
from toystudio.engine.analysis.scenes import detect_cuts
from toystudio.engine.analysis.single_pass import FfmpegFailed, Outputs, Stopped, outputs_for
from toystudio.engine.analysis.sync import sync_tracks
from toystudio.engine.media.probe import check_integrity, probe_media
from toystudio.engine.media.thumbs import card_thumbnail, frame_at
from toystudio.engine.moments.group import group_ids
from toystudio.engine.moments.rules import find_moments
from toystudio.engine.moments.signals import load_signals
from toystudio.jobs.import_job import restore_missing
from toystudio.jobs.protocol import Event, Progress
from toystudio.storage.app_store import AppStore
from toystudio.storage.project_store import ProjectStore

if TYPE_CHECKING:
    from toystudio.jobs.worker_main import JobContext

STAGE_VERSION: dict[StageName, int] = {s: 1 for s in (*STAGE_ORDER, StageName.SYNC)}
# phase 2a wrote SKIPPED rows with version 1 for these two; 2 makes them run on old projects
STAGE_VERSION[StageName.SPEECH] = 2
STAGE_VERSION[StageName.MOMENTS] = 2
SPEED_STAGE = "proxy"
SPEED_ALPHA = 0.2
ETA_EVERY_S = 2.0
PROJECT_MEDIA_ID = 0  # project-level stages (SYNC) are stored with media_id 0


class _SkipFile(Exception):
    """This file cannot go further (missing, corrupt, ffmpeg failed); go to the next one."""


@dataclass
class _Run:
    ctx: JobContext
    store: ProjectStore
    app: AppStore | None
    speeds: dict[str, float]
    proxy_left_s: dict[int, tuple[str, float]] = field(default_factory=dict)
    speech_left_s: dict[int, float] = field(default_factory=dict)
    last_eta: float = float("-inf")

    @property
    def cache(self) -> Path:
        return self.store.root / "cache"

    # --- events ---

    def stage_event(
        self, media_id: int, stage: StageName, state: StageState, found: int | None = None
    ) -> None:
        payload = {"media_id": media_id, "stage": stage.value, "state": state.value}
        self.ctx.report(Event("stage", {**payload, "found": found}))

    def finish(self, media_id: int, stage: StageName, state: StageState, *,
               found: int | None = None, error_code: str | None = None) -> None:  # fmt: skip
        self.store.set_stage(
            media_id,
            stage,
            state,
            progress=1.0 if state is StageState.DONE else 0.0,
            version=STAGE_VERSION[stage],
            error_code=error_code,
        )
        if state is StageState.DONE and stage is not StageName.PROXY:
            self.ctx.report(Progress(stage.value, 1, 1, str(media_id)))
        self.stage_event(media_id, stage, state, found)

    def eta(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_eta < ETA_EVERY_S:
            return
        self.last_eta = now
        left = project_remaining_s(list(self.proxy_left_s.values()), self.speeds)
        left += sum(self.speech_left_s.values()) / SPEECH_X_RT
        self.ctx.report(Event("eta", {"remaining_s": float(left)}))

    # --- speeds ---

    def record_speed(self, info: MediaInfo, media_s: float, wall_s: float) -> None:
        if wall_s <= 0 or media_s <= 0:
            return
        key = profile_key(info)
        measured = media_s / wall_s
        old = self.app.speed(SPEED_STAGE, key) if self.app is not None else None
        value = measured if old is None else (1 - SPEED_ALPHA) * old + SPEED_ALPHA * measured
        if self.app is not None:
            self.app.set_speed(SPEED_STAGE, key, value)
        self.speeds[key] = value

    # --- stages ---

    def is_current(self, media_id: int, stage: StageName) -> bool:
        st = self.store.stage(media_id, stage)
        return (
            st is not None
            and st.state in (StageState.DONE, StageState.SKIPPED)
            and st.version == STAGE_VERSION[stage]
        )

    def media(self, media_id: int) -> MediaRow:
        return next(m for m in self.store.list_media() if m.id == media_id)

    def analyze_file(self, media_id: int) -> None:
        ran = False  # an earlier stage ran in this job: MOMENTS must see its new output
        for stage in STAGE_ORDER:
            if self.is_current(media_id, stage) and not (ran and stage is StageName.MOMENTS):
                continue
            ran = True
            row = self.media(media_id)
            if not row.path.is_file():
                self.store.set_media_status(media_id, MediaStatus.MISSING)
                self.ctx.report(Event("media", {"media_id": media_id, "status": "missing"}))
                self.drop_eta(media_id)
                raise _SkipFile()
            try:
                if self.ctx.should_stop():
                    raise Stopped()
                self.store.set_stage(media_id, stage, StageState.RUNNING)
                self.stage_event(media_id, stage, StageState.RUNNING)
                self.run_stage(row, stage)
            except Stopped:
                self.store.set_stage(media_id, stage, StageState.INTERRUPTED)
                self.stage_event(media_id, stage, StageState.INTERRUPTED)
                raise
            except _SkipFile:
                raise
            except Exception:  # noqa: BLE001 - one bad file must not stop the others
                traceback.print_exc(file=sys.stderr)
                self.drop_eta(media_id)
                self.finish(media_id, stage, StageState.FAILED, error_code="error")
                raise _SkipFile() from None

    def drop_eta(self, media_id: int) -> None:
        self.proxy_left_s.pop(media_id, None)
        self.speech_left_s.pop(media_id, None)

    def run_stage(self, row: MediaRow, stage: StageName) -> None:
        out = outputs_for(self.cache, row.id)
        if stage in _VIDEO_ONLY and _audio_only(row):
            self.finish(row.id, stage, StageState.SKIPPED)
        elif stage is StageName.PROBE:
            self.probe(row)
        elif stage is StageName.PROXY:
            self.proxy(row, out)
        elif stage is StageName.SPEECH:
            self.speech(row, out)
        elif stage is StageName.SCENES:
            cuts = detect_cuts(_metrics(out), fps=single_pass.METRIC_FPS)
            self.store.replace_scene_cuts(row.id, [c for c, _ in cuts], [s for _, s in cuts])
            self.finish(row.id, stage, StageState.DONE, found=len(cuts))
        elif stage is StageName.QUALITY:
            intervals = bad_intervals(row.id, _metrics(out), fps=single_pass.METRIC_FPS)
            self.store.replace_quality(row.id, intervals)
            self.finish(row.id, stage, StageState.DONE, found=len(intervals))
        elif stage is StageName.MOMENTS:
            self.moments(row, out)
        else:  # pragma: no cover - STAGE_ORDER is closed
            raise ValueError(f"unexpected per-file stage {stage}")

    def probe(self, row: MediaRow) -> None:
        info = row.info
        if info is None:
            try:
                info = probe_media(row.path) if check_integrity(row.path) else None
            except ValueError:
                info = None
            if info is not None:
                self.store.set_media_info(row.id, info)
        usable = info is not None and (info.has_audio if _audio_only(row) else info.has_video)
        if info is None or not usable:
            self.store.set_media_status(row.id, MediaStatus.CORRUPT)
            self.finish(row.id, StageName.PROBE, StageState.FAILED, error_code="corrupt")
            self.ctx.report(Event("media", {"media_id": row.id, "status": "corrupt"}))
            self.drop_eta(row.id)
            raise _SkipFile()
        if not _audio_only(row):
            self.proxy_left_s[row.id] = (profile_key(info), info.duration_us / US_PER_S)
        if info.has_audio and not self.is_current(row.id, StageName.SPEECH):
            self.speech_left_s[row.id] = info.duration_us / US_PER_S
        self.finish(row.id, StageName.PROBE, StageState.DONE)

    def proxy(self, row: MediaRow, out: Outputs) -> None:
        info = row.info
        assert info is not None  # PROBE is done before PROXY
        if _audio_only(row):
            self.audio_pass(row, info, out)
            return
        if not (out.proxy.is_file() and out.frames.is_file()):  # proxy = completion marker
            total_s = info.duration_us / US_PER_S
            key = profile_key(info)

            def on_progress(done_s: float) -> None:
                self.ctx.report(Progress(StageName.PROXY.value, done_s, total_s, str(row.id)))
                self.proxy_left_s[row.id] = (key, max(total_s - done_s, 0.0))
                self.eta()

            started = time.monotonic()
            try:
                single_pass.run(
                    row.path, info, out, on_progress=on_progress, should_stop=self.ctx.should_stop
                )
            except FfmpegFailed:
                self.drop_eta(row.id)
                self.finish(row.id, StageName.PROXY, StageState.FAILED, error_code="ffmpeg")
                raise _SkipFile() from None
            self.record_speed(info, total_s, time.monotonic() - started)
        self.proxy_left_s.pop(row.id, None)
        self.finish(row.id, StageName.PROXY, StageState.DONE)
        self.eta(force=True)
        self.card(out.proxy)

    def audio_pass(self, row: MediaRow, info: MediaInfo, out: Outputs) -> None:
        """Speech recording: wav and envelope only (no picture, nothing to time for the ETA)."""
        if not (out.wav.is_file() and out.envelope.is_file()):
            total_s = info.duration_us / US_PER_S

            def on_progress(done_s: float) -> None:
                self.ctx.report(Progress(StageName.PROXY.value, done_s, total_s, str(row.id)))

            try:
                single_pass.run_audio(
                    row.path, info, out, on_progress=on_progress, should_stop=self.ctx.should_stop
                )
            except FfmpegFailed:
                self.drop_eta(row.id)
                self.finish(row.id, StageName.PROXY, StageState.FAILED, error_code="ffmpeg")
                raise _SkipFile() from None
        self.finish(row.id, StageName.PROXY, StageState.DONE)

    def speech(self, row: MediaRow, out: Outputs) -> None:
        """Stage 2: speech regions (VAD) for the first half of the progress, events second."""
        # imported here: an onnxruntime/model problem fails this stage, not every worker job
        from toystudio.engine.audio.events import detect_events
        from toystudio.engine.audio.vad import detect_speech

        if not out.wav.is_file():  # the file has no audio
            self.speech_left_s.pop(row.id, None)
            self.finish(row.id, StageName.SPEECH, StageState.SKIPPED)
            return
        info = row.info
        assert info is not None  # PROBE is done before SPEECH
        total_s = info.duration_us / US_PER_S

        def progress(part: int) -> Callable[[float], None]:
            def on_progress(done_s: float) -> None:
                done = min((part + min(done_s, total_s) / total_s) / 2, 1.0) * total_s
                self.ctx.report(Progress(StageName.SPEECH.value, done, total_s, str(row.id)))
                self.speech_left_s[row.id] = max(total_s - done, 0.0)
                self.eta()

            return on_progress

        regions = detect_speech(out.wav, on_progress=progress(0), should_stop=self.ctx.should_stop)
        evs = detect_events(
            row.id, out.wav, on_progress=progress(1), should_stop=self.ctx.should_stop
        )
        self.store.replace_audio(row.id, regions, evs)
        self.ctx.report(Progress(StageName.SPEECH.value, total_s, total_s, str(row.id)))
        self.speech_left_s.pop(row.id, None)
        self.finish(row.id, StageName.SPEECH, StageState.DONE, found=len(regions))
        self.eta(force=True)

    def moments(self, row: MediaRow, out: Outputs) -> None:
        """Stage 5: moment rules over the file's signals, a preview frame for each moment."""
        found = find_moments(load_signals(self.store, self.cache, row.id))
        old = [m.preview for m in self.store.moments(row.id) if m.preview]
        ids = self.store.replace_moments(row.id, found)
        for rel in old:  # ids are never reused, so old previews are never the new ones
            (self.store.root / rel).unlink(missing_ok=True)
        previews: list[str | None] = []
        for mid, m in zip(ids, found, strict=True):
            if self.ctx.should_stop():
                raise Stopped()
            previews.append(self.preview(out.proxy, mid, (m.start_us + m.end_us) // 2))
        self.store.set_previews(list(zip(ids, previews, strict=True)))
        for mid, m, preview in zip(ids, found, previews, strict=True):
            payload = {"media_id": row.id, "moment_id": mid, "type": m.type.value,
                       "start_us": m.start_us, "end_us": m.end_us, "score": m.score,
                       "preview": preview,
                       "quality_label": m.quality_label.value,
                       "reasons": [{"code": r.code, "ru": r.ru, "ky": r.ky} for r in m.reasons]}  # fmt: skip
            self.ctx.report(Event("moment", payload))
        self.finish(row.id, StageName.MOMENTS, StageState.DONE, found=len(found))

    def preview(self, proxy: Path, moment_id: int, at_us: int) -> str | None:
        rel = f"cache/thumbs/m{moment_id}.jpg"
        try:
            frame_at(proxy, at_us, self.store.root / rel)
        except (RuntimeError, ValueError, OSError):
            traceback.print_exc(file=sys.stderr)
            return None  # a missing preview must not fail the stage
        return rel

    def card(self, proxy: Path) -> None:
        card = self.cache / "thumbs" / "card.jpg"
        if card.exists():
            return
        try:
            card_thumbnail(proxy, card)
        except (RuntimeError, ValueError, OSError):
            pass  # a missing card picture must not fail the analysis

    def sync(self, media: list[MediaRow]) -> None:
        stage = StageName.SYNC
        if self.ctx.should_stop():
            self.store.set_stage(PROJECT_MEDIA_ID, stage, StageState.INTERRUPTED)
            raise Stopped()
        self.store.set_stage(PROJECT_MEDIA_ID, stage, StageState.RUNNING)
        self.stage_event(PROJECT_MEDIA_ID, stage, StageState.RUNNING)
        try:

            def env(mid: int) -> np.ndarray | None:
                path = outputs_for(self.cache, mid).envelope
                return np.load(path) if path.is_file() else None

            def track(role: MediaRole) -> list[MediaRow]:
                return sorted((m for m in media if m.role is role), key=lambda m: m.path.name)

            for m in track(MediaRole.PRE):  # mixed shooting at different times: no common clock
                self.store.set_sync(m.id, None, None)
            found = sync_tracks(
                {m.id: env(m.id) for m in track(MediaRole.MAIN)},
                {m.id: env(m.id) for m in track(MediaRole.CUTAWAY)},
                [(m.id, env(m.id)) for m in track(MediaRole.SPEECH)],  # recorder order = names
            )
            for r in found:
                self.store.set_sync(r.media_id, r.offset_us, r.confidence)
        except Exception:  # noqa: BLE001 - files stay analysed; the wizard can finish
            traceback.print_exc(file=sys.stderr)
            self.finish(PROJECT_MEDIA_ID, stage, StageState.FAILED, error_code="error")
            return
        self.finish(PROJECT_MEDIA_ID, stage, StageState.DONE)
        self.group()

    def group(self) -> None:
        """The same moment seen by several cameras shares a group (needs sync offsets)."""
        try:
            ok = {m.id: m.sync_offset_us for m in _ok_videos(self.store)}
            moments = [m for m in self.store.moments() if m.media_id in ok]
            self.store.set_group_ids(group_ids(moments, ok))
        except Exception:  # noqa: BLE001 - moments stay usable without groups
            traceback.print_exc(file=sys.stderr)


def _metrics(out: Outputs) -> dict[str, np.ndarray]:
    with np.load(out.frames) as z:
        return {k: z[k] for k in z.files}


def _speeds(app: AppStore | None) -> dict[str, float]:
    speeds = dict(DEFAULT_X_RT)
    if app is not None:
        for key in speeds:
            stored = app.speed(SPEED_STAGE, key)
            if stored is not None and stored > 0:
                speeds[key] = stored
    return speeds


def _ok_videos(store: ProjectStore) -> list[MediaRow]:
    """Analysable files (videos of every track and speech recordings) that are present."""
    return [m for m in store.analyzable_media() if m.status is MediaStatus.OK]


_VIDEO_ONLY = (StageName.SCENES, StageName.QUALITY, StageName.MOMENTS)


def _audio_only(row: MediaRow) -> bool:
    return row.kind is MediaKind.MUSIC


def run(ctx: JobContext) -> None:
    store = ProjectStore.open(Path(ctx.project))
    app = AppStore(ctx.app_db) if ctx.app_db is not None else None
    try:
        r = _Run(ctx, store, app, _speeds(app))
        for mid in restore_missing(store):  # finished stages of a returned file are reused
            ctx.report(Event("media", {"media_id": mid, "status": "ok"}))
        videos = _ok_videos(store)
        for m in videos:  # pending rows up front, so the GUI shows the whole plan
            for stage in STAGE_ORDER:
                if store.stage(m.id, stage) is None:
                    store.set_stage(m.id, stage, StageState.PENDING, version=STAGE_VERSION[stage])
            if (
                m.info is not None
                and not _audio_only(m)
                and not r.is_current(m.id, StageName.PROXY)
            ):
                r.proxy_left_s[m.id] = (profile_key(m.info), m.info.duration_us / US_PER_S)
            if m.info is not None and m.info.has_audio and not r.is_current(m.id, StageName.SPEECH):
                r.speech_left_s[m.id] = m.info.duration_us / US_PER_S
        if store.stage(PROJECT_MEDIA_ID, StageName.SYNC) is None:
            store.set_stage(PROJECT_MEDIA_ID, StageName.SYNC, StageState.PENDING)
        r.eta(force=True)
        for m in videos:
            try:
                r.analyze_file(m.id)
            except _SkipFile:
                continue
        r.sync(_ok_videos(store))
    finally:
        if app is not None:
            app.close()
        store.close()
