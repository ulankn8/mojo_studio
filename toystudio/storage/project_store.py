"""Per-project SQLite store (project.db) and folder layout."""

import json
import os
import sqlite3
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Self

from toystudio.core.models import (
    STAGE_ORDER,
    AudioEvent,
    AudioLabel,
    Mark,
    MediaInfo,
    MediaKind,
    MediaRole,
    MediaRow,
    MediaStatus,
    MomentRow,
    MomentType,
    ProjectFormat,
    QualityInterval,
    QualityLabel,
    Reason,
    SpeechRegion,
    StageName,
    StageRun,
    StageState,
    ToiKind,
)
from toystudio.core.timeline import Timeline
from toystudio.storage.db import _migrations, connect, migrate
from toystudio.storage.serde import timeline_from_json, timeline_to_json

KEEP_SNAPSHOTS = 50
CACHE_DIRS = ("proxy", "audio", "frames", "thumbs", "wave", "export")
_PKG = "toystudio.storage"


def _now() -> str:
    return datetime.now().isoformat()


def readonly_uri(db: Path | str, pathname2url: Callable[[str], str] | None = None) -> str:
    """SQLite URI that opens `db` read-only (works for mapped and network drives).

    Path.resolve()/as_uri() turn a mapped drive Z: into \\\\server\\share and then into
    file://server/share, whose authority SQLite rejects. So the path stays absolute but
    unresolved, and a UNC path gets an empty authority: file:////server/share/...
    """
    to_url = pathname2url or urllib.request.pathname2url
    url = to_url(os.path.abspath(db))
    if url.startswith("//") and not url.startswith("///"):
        url = "//" + url  # UNC: empty authority, path //server/share
    return f"file:{url}?mode=ro"


@dataclass(frozen=True)
class ProjectSummary:
    """Read-only snapshot of a project for lists; never touches the project's state."""

    root: Path
    name: str
    kind: ToiKind
    event_date: date | None
    duration_us: int
    step: Literal["files", "analysis", "analysis_done"]
    has_copies: bool = False
    format: ProjectFormat = ProjectFormat.TOI


class ProjectStore:
    def __init__(self, root: Path, con: sqlite3.Connection) -> None:
        self.root = root
        self._con = con

    @classmethod
    def create(
        cls,
        root: Path,
        name: str,
        kind: ToiKind,
        event_date: date,
        fmt: ProjectFormat = ProjectFormat.TOI,
    ) -> Self:
        root.mkdir(parents=True, exist_ok=True)
        for d in CACHE_DIRS:
            (root / "cache" / d).mkdir(parents=True, exist_ok=True)
        con = connect(root / "project.db")
        try:
            migrate(con, _PKG, root / "project.db")
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
            now = _now()
            with con:
                con.execute(
                    "INSERT INTO project(id, name, kind, event_date, created_at, updated_at,"
                    " schema_version, format) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                    (name, kind.value, event_date.isoformat(), now, now, version, fmt.value),
                )
        except BaseException:
            con.close()
            raise
        return cls(root, con)

    @classmethod
    def open(cls, root: Path) -> Self:
        db = root / "project.db"
        if not db.is_file():
            raise FileNotFoundError(db)
        con = connect(db)
        try:
            migrate(con, _PKG, db)
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
            with con:
                con.execute("UPDATE project SET schema_version=? WHERE id=1", (version,))
                con.execute("UPDATE job SET state='interrupted' WHERE state='running'")
                con.execute("UPDATE stage_run SET state='interrupted' WHERE state='running'")
        except BaseException:
            con.close()
            raise
        return cls(root, con)

    @classmethod
    def open_readonly(cls, root: Path) -> Self:
        """Open for reading only: no migrations, no state changes (safe while a worker runs).

        Methods that write raise sqlite3.OperationalError.
        """
        db = root / "project.db"
        if not db.is_file():
            raise FileNotFoundError(db)
        return cls(root, sqlite3.connect(readonly_uri(db), uri=True))

    @classmethod
    def peek(cls, root: Path, fallback_name: str = "") -> ProjectSummary:
        """Summarise a project without migrating or writing anything (safe while a worker runs).

        If project.db is missing, unreadable or older than the current schema, returns the
        fallback name, kind OTHER and step "files".
        """
        fallback = ProjectSummary(root, fallback_name or root.name, ToiKind.OTHER, None, 0, "files")
        db = root / "project.db"
        if not db.is_file():
            return fallback
        current = max(n for n, _ in _migrations(_PKG))
        try:
            con = sqlite3.connect(readonly_uri(db), uri=True)
        except sqlite3.Error:
            return fallback
        try:
            if int(con.execute("PRAGMA user_version").fetchone()[0]) < current:
                return fallback
            row = con.execute(
                "SELECT name, kind, event_date, format FROM project WHERE id=1"
            ).fetchone()
            if row is None:
                return fallback
            store = cls(root, con)
            media = store.list_media(MediaKind.VIDEO)
            duration = sum(
                m.info.duration_us for m in media if m.status is MediaStatus.OK and m.info
            )
            copies = any(m.copied for m in store.list_media())
            return ProjectSummary(
                root,
                str(row[0]),
                ToiKind(row[1]),
                date.fromisoformat(row[2]) if row[2] else None,
                duration,
                store.step(),
                copies,
                ProjectFormat(row[3]) if row[3] else ProjectFormat.TOI,
            )
        except (sqlite3.Error, ValueError):
            return fallback
        finally:
            con.close()

    def close(self) -> None:
        self._con.close()

    def rename(self, name: str) -> None:
        with self._con:
            self._con.execute("UPDATE project SET name=?, updated_at=? WHERE id=1", (name, _now()))

    def update_details(self, kind: ToiKind, event_date: date) -> None:
        with self._con:
            self._con.execute(
                "UPDATE project SET kind=?, event_date=?, updated_at=? WHERE id=1",
                (kind.value, event_date.isoformat(), _now()),
            )

    @property
    def name(self) -> str:
        return str(self._con.execute("SELECT name FROM project WHERE id=1").fetchone()[0])

    def create_cut(self, title: str, aspect: str, target_us: int) -> str:
        cut_id = uuid.uuid4().hex
        with self._con:
            self._con.execute(
                "INSERT INTO cut(id, title, created_at, aspect, target_duration_us)"
                " VALUES (?, ?, ?, ?, ?)",
                (cut_id, title, _now(), aspect, target_us),
            )
        return cut_id

    def save_timeline(self, cut_id: str, t: Timeline) -> int:
        with self._con:
            seq = int(
                self._con.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM timeline_snapshot WHERE cut_id=?",
                    (cut_id,),
                ).fetchone()[0]
            )
            self._con.execute(
                "INSERT INTO timeline_snapshot(cut_id, seq, json, created_at) VALUES (?, ?, ?, ?)",
                (cut_id, seq, timeline_to_json(t), _now()),
            )
            self._con.execute(
                "DELETE FROM timeline_snapshot WHERE cut_id=? AND seq <= ?",
                (cut_id, seq - KEEP_SNAPSHOTS),
            )
        return seq

    def load_timeline(self, cut_id: str) -> Timeline | None:
        row = self._con.execute(
            "SELECT json FROM timeline_snapshot WHERE cut_id=? ORDER BY seq DESC LIMIT 1",
            (cut_id,),
        ).fetchone()
        return None if row is None else timeline_from_json(row[0])

    def snapshot_count(self, cut_id: str) -> int:
        return int(
            self._con.execute(
                "SELECT COUNT(*) FROM timeline_snapshot WHERE cut_id=?", (cut_id,)
            ).fetchone()[0]
        )

    def set_job_state(self, job_id: str, state: str) -> None:
        with self._con:
            self._con.execute(
                "INSERT INTO job(id, state, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
                " updated_at=excluded.updated_at",
                (job_id, state, _now()),
            )

    def job_state(self, job_id: str) -> str:
        row = self._con.execute("SELECT state FROM job WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return str(row[0])

    # --- media ---

    @property
    def format(self) -> ProjectFormat:
        row = self._con.execute("SELECT format FROM project WHERE id=1").fetchone()
        return ProjectFormat(row[0]) if row is not None and row[0] else ProjectFormat.TOI

    def add_media(
        self, path: Path, role: MediaRole | MediaKind, size: int, copied: bool
    ) -> tuple[int, bool]:
        if isinstance(role, MediaKind):
            role = MediaRole.default_for(role)
        resolved = path.resolve()
        for row in self._con.execute("SELECT id, path, size FROM media").fetchall():
            other = Path(row[1])
            if other.resolve() == resolved or (other.name == path.name and row[2] == size):
                return int(row[0]), False
        with self._con:
            cur = self._con.execute(
                "INSERT INTO media(kind, role, path, copied, size, added_at, camera_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    role.kind.value,
                    role.value,
                    str(path),
                    int(copied),
                    size,
                    _now(),
                    str(path.parent),
                ),
            )
        return int(cur.lastrowid or 0), True

    def set_media_info(self, media_id: int, info: MediaInfo) -> None:
        with self._con:
            self._con.execute(
                "UPDATE media SET duration_us=?, width=?, height=?, rotation=?, fps=?, codec=?,"
                " hdr=?, has_audio=?, has_video=?, bit_rate=?, creation_time=? WHERE id=?",
                (
                    info.duration_us,
                    info.width,
                    info.height,
                    info.rotation,
                    info.fps,
                    info.codec,
                    int(info.hdr),
                    int(info.has_audio),
                    int(info.has_video),
                    info.bit_rate,
                    info.creation_time,
                    media_id,
                ),
            )

    def set_media_status(self, media_id: int, status: MediaStatus) -> None:
        with self._con:
            self._con.execute("UPDATE media SET status=? WHERE id=?", (status.value, media_id))

    def set_media_path(self, media_id: int, path: Path, copied: bool) -> None:
        with self._con:
            self._con.execute(
                "UPDATE media SET path=?, copied=? WHERE id=?", (str(path), int(copied), media_id)
            )

    def list_media(self, kind: MediaKind | None = None) -> list[MediaRow]:
        sql = (
            "SELECT id, kind, path, copied, size, duration_us, width, height, rotation, fps,"
            " codec, hdr, has_audio, has_video, bit_rate, creation_time, status,"
            " sync_offset_us, sync_confidence, role, camera_id FROM media"
        )
        args: tuple[str, ...] = ()
        if kind is not None:
            sql += " WHERE kind=?"
            args = (kind.value,)
        rows = self._con.execute(sql + " ORDER BY id", args).fetchall()
        out: list[MediaRow] = []
        for r in rows:
            info = None
            if r[5] is not None:
                info = MediaInfo(
                    duration_us=int(r[5]),
                    width=int(r[6] or 0),
                    height=int(r[7] or 0),
                    rotation=int(r[8] or 0),
                    fps=float(r[9] or 0.0),
                    codec=str(r[10] or ""),
                    hdr=bool(r[11]),
                    has_audio=bool(r[12]),
                    has_video=bool(r[13]),
                    bit_rate=int(r[14] or 0),
                    creation_time=r[15],
                )
            out.append(
                MediaRow(
                    id=int(r[0]),
                    kind=MediaKind(r[1]),
                    path=Path(r[2]),
                    copied=bool(r[3]),
                    size=int(r[4] or 0),
                    info=info,
                    status=MediaStatus(r[16]),
                    sync_offset_us=r[17],
                    sync_confidence=r[18],
                    role=MediaRole(r[19]) if r[19] else MediaRole.default_for(MediaKind(r[1])),
                    camera=r[20] or str(Path(r[2]).parent),
                )
            )
        return out

    def camera_clock(self) -> dict[str, int]:
        """Clock corrections in us per camera, set by matching one pair of clips."""
        rows = self._con.execute("SELECT camera_id, offset_us FROM camera_clock").fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def set_camera_clock(self, camera: str, offset_us: int) -> None:
        with self._con:
            self._con.execute(
                "INSERT INTO camera_clock(camera_id, offset_us) VALUES (?, ?)"
                " ON CONFLICT(camera_id) DO UPDATE SET offset_us=excluded.offset_us",
                (camera, offset_us),
            )

    def analyzable_media(self) -> list[MediaRow]:
        """Files the analysis works on: videos of every role and speech recordings."""
        return [m for m in self.list_media() if m.analyzable]

    def remove_media(self, media_id: int) -> None:
        with self._con:
            # stage_run has no FK (media_id 0 = project stages), so delete explicitly
            self._con.execute("DELETE FROM stage_run WHERE media_id=?", (media_id,))
            self._con.execute("DELETE FROM media WHERE id=?", (media_id,))

    # --- stages ---

    def stage(self, media_id: int, stage: StageName) -> StageRun | None:
        row = self._con.execute(
            "SELECT media_id, stage, state, progress, version, error_code FROM stage_run"
            " WHERE media_id=? AND stage=?",
            (media_id, stage.value),
        ).fetchone()
        return None if row is None else _stage_row(row)

    def set_stage(
        self,
        media_id: int,
        stage: StageName,
        state: StageState,
        *,
        progress: float = 0.0,
        version: int = 1,
        error_code: str | None = None,
    ) -> None:
        finished = state in (StageState.DONE, StageState.FAILED, StageState.SKIPPED)
        with self._con:
            self._con.execute(
                "INSERT INTO stage_run(media_id, stage, state, progress, error_code, finished_at,"
                " version) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(media_id, stage) DO UPDATE SET state=excluded.state,"
                " progress=excluded.progress, error_code=excluded.error_code,"
                " finished_at=excluded.finished_at, version=excluded.version",
                (
                    media_id,
                    stage.value,
                    state.value,
                    progress,
                    error_code,
                    _now() if finished else None,
                    version,
                ),
            )

    def stages(self) -> list[StageRun]:
        rows = self._con.execute(
            "SELECT media_id, stage, state, progress, version, error_code FROM stage_run"
            " ORDER BY media_id, stage"
        ).fetchall()
        return [_stage_row(r) for r in rows]

    # --- analysis results ---

    def replace_scene_cuts(self, media_id: int, cuts_us: list[int], scores: list[float]) -> None:
        if len(cuts_us) != len(scores):
            raise ValueError("cuts_us and scores must have the same length")
        with self._con:
            self._con.execute("DELETE FROM scene_cut WHERE media_id=?", (media_id,))
            self._con.executemany(
                "INSERT INTO scene_cut(media_id, at_us, score) VALUES (?, ?, ?)",
                [(media_id, c, s) for c, s in zip(cuts_us, scores, strict=True)],
            )

    def scene_cut_count(self, media_id: int | None = None) -> int:
        if media_id is None:
            row = self._con.execute("SELECT COUNT(*) FROM scene_cut").fetchone()
        else:
            row = self._con.execute(
                "SELECT COUNT(*) FROM scene_cut WHERE media_id=?", (media_id,)
            ).fetchone()
        return int(row[0])

    def scene_cuts(self, media_id: int) -> list[int]:
        rows = self._con.execute(
            "SELECT at_us FROM scene_cut WHERE media_id=? ORDER BY at_us", (media_id,)
        ).fetchall()
        return [int(r[0]) for r in rows]

    def replace_quality(self, media_id: int, intervals: list[QualityInterval]) -> None:
        """Store problem intervals; CLEAR is the absence of a row and is not persisted."""
        with self._con:
            self._con.execute("DELETE FROM quality_interval WHERE media_id=?", (media_id,))
            self._con.executemany(
                "INSERT INTO quality_interval(media_id, start_us, end_us, label)"
                " VALUES (?, ?, ?, ?)",
                [
                    (media_id, i.start_us, i.end_us, i.label.value)
                    for i in intervals
                    if i.label is not QualityLabel.CLEAR
                ],
            )

    def quality_intervals(self, media_id: int) -> list[QualityInterval]:
        rows = self._con.execute(
            "SELECT media_id, start_us, end_us, label FROM quality_interval"
            " WHERE media_id=? ORDER BY start_us",
            (media_id,),
        ).fetchall()
        return [QualityInterval(int(r[0]), int(r[1]), int(r[2]), QualityLabel(r[3])) for r in rows]

    def set_sync(self, media_id: int, offset_us: int | None, confidence: float | None) -> None:
        with self._con:
            self._con.execute(
                "UPDATE media SET sync_offset_us=?, sync_confidence=? WHERE id=?",
                (offset_us, confidence, media_id),
            )

    # --- audio events, speech regions and moments ---

    def replace_audio(
        self, media_id: int, regions: list[SpeechRegion], events: list[AudioEvent]
    ) -> None:
        with self._con:
            self._con.execute("DELETE FROM speech_region WHERE media_id=?", (media_id,))
            self._con.execute("DELETE FROM audio_event WHERE media_id=?", (media_id,))
            self._con.executemany(
                "INSERT INTO speech_region(media_id, start_us, end_us, prob) VALUES (?, ?, ?, ?)",
                [(media_id, r.start_us, r.end_us, r.prob) for r in regions],
            )
            self._con.executemany(
                "INSERT INTO audio_event(media_id, start_us, end_us, label, score)"
                " VALUES (?, ?, ?, ?, ?)",
                [(media_id, e.start_us, e.end_us, e.label.value, e.score) for e in events],
            )

    def speech_regions(self, media_id: int) -> list[SpeechRegion]:
        rows = self._con.execute(
            "SELECT start_us, end_us, prob FROM speech_region WHERE media_id=?"
            " ORDER BY start_us, end_us",
            (media_id,),
        ).fetchall()
        return [SpeechRegion(int(r[0]), int(r[1]), float(r[2] or 0.0)) for r in rows]

    def audio_events(self, media_id: int) -> list[AudioEvent]:
        rows = self._con.execute(
            "SELECT media_id, start_us, end_us, label, score FROM audio_event WHERE media_id=?"
            " ORDER BY start_us, end_us, label",
            (media_id,),
        ).fetchall()
        return [
            AudioEvent(int(r[0]), int(r[1]), int(r[2]), AudioLabel(r[3]), float(r[4] or 0.0))
            for r in rows
        ]

    def replace_moments(self, media_id: int, moments: list[MomentRow]) -> list[int]:
        """Replace a file's moments; returns the new ids in input order.

        A new moment inherits mark and user_text from an old moment of the same type whose
        overlap is at least 50 % of the shorter of the two (largest overlap wins); an old
        moment is inherited by at most one new moment. Only the database is touched.
        """
        with self._con:
            old = self._con.execute(
                "SELECT id, type, start_us, end_us, mark, user_text, transcript_excerpt FROM moment"
                " WHERE media_id=?",
                (media_id,),
            ).fetchall()
            free = list(old)
            self._con.execute("DELETE FROM moment WHERE media_id=?", (media_id,))
            ids: list[int] = []
            for m in moments:
                best = None
                best_overlap = 0
                for o in free:
                    if o[1] != m.type.value:
                        continue
                    overlap = min(m.end_us, int(o[3])) - max(m.start_us, int(o[2]))
                    shorter = min(m.end_us - m.start_us, int(o[3]) - int(o[2]))
                    if overlap > 0 and overlap * 2 >= shorter and overlap > best_overlap:
                        best, best_overlap = o, overlap
                mark, user_text, excerpt = (m.mark.value, None, None)
                if best is not None:
                    free.remove(best)
                    mark, user_text, excerpt = best[4], best[5], best[6]
                cur = self._con.execute(
                    "INSERT INTO moment(media_id, type, start_us, end_us, score, quality_label,"
                    " user_text, transcript_excerpt, mark, group_id, reasons_json, preview)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        media_id,
                        m.type.value,
                        m.start_us,
                        m.end_us,
                        m.score,
                        m.quality_label.value,
                        user_text,
                        excerpt,
                        mark,
                        m.group_id,
                        _reasons_to_json(m.reasons),
                        m.preview,
                    ),
                )
                ids.append(int(cur.lastrowid or 0))
        return ids

    def moments(self, media_id: int | None = None) -> list[MomentRow]:
        sql = (
            "SELECT id, media_id, type, start_us, end_us, score, quality_label, reasons_json,"
            " mark, group_id, preview FROM moment"
        )
        args: tuple[int, ...] = ()
        if media_id is not None:
            sql += " WHERE media_id=?"
            args = (media_id,)
        rows = self._con.execute(sql + " ORDER BY media_id, start_us, id", args).fetchall()
        return [
            MomentRow(
                id=int(r[0]),
                media_id=int(r[1]),
                type=MomentType(r[2]),
                start_us=int(r[3]),
                end_us=int(r[4]),
                score=float(r[5] or 0.0),
                quality_label=QualityLabel(r[6] or QualityLabel.CLEAR.value),
                reasons=_reasons_from_json(r[7]),
                mark=Mark(r[8]),
                group_id=None if r[9] is None else int(r[9]),
                preview=None if r[10] is None else str(r[10]),
            )
            for r in rows
        ]

    def set_group_ids(self, pairs: list[tuple[int, int | None]]) -> None:
        with self._con:
            self._con.executemany(
                "UPDATE moment SET group_id=? WHERE id=?", [(g, mid) for mid, g in pairs]
            )

    def set_previews(self, pairs: list[tuple[int, str | None]]) -> None:
        """(moment id, preview path relative to the project root)."""
        with self._con:
            self._con.executemany(
                "UPDATE moment SET preview=? WHERE id=?", [(p, mid) for mid, p in pairs]
            )

    def set_mark(self, moment_id: int, mark: Mark) -> None:
        with self._con:
            self._con.execute("UPDATE moment SET mark=? WHERE id=?", (mark.value, moment_id))

    def moment_counts(self) -> dict[MomentType, int]:
        """Moments per type over files with status OK."""
        rows = self._con.execute(
            "SELECT m.type, COUNT(*) FROM moment m JOIN media f ON f.id = m.media_id"
            " WHERE f.status=? GROUP BY m.type",
            (MediaStatus.OK.value,),
        ).fetchall()
        return {MomentType(r[0]): int(r[1]) for r in rows}

    # --- wizard step ---

    def step(self) -> Literal["files", "analysis", "analysis_done"]:
        """Wizard step of the project.

        "files" while there is no video or analysis never started: no per-file stage row
        (ruling R21; `analyze` writes PENDING rows for every OK video up front, `remove_media`
        drops a file's rows, the project-level SYNC row does not count). A MISSING video keeps
        the project in "analysis". A file with a FAILED stage counts as finished (ruling R9), so does a
        FAILED SYNC.
        """
        all_videos = self.analyzable_media()
        started = (
            self._con.execute("SELECT 1 FROM stage_run WHERE media_id != 0 LIMIT 1").fetchone()
            is not None
        )
        if not all_videos or not started:
            return "files"
        if any(m.status is MediaStatus.MISSING for m in all_videos):
            return "analysis"  # a disconnected drive: the analysis is not complete yet
        videos = [m for m in all_videos if m.status is MediaStatus.OK]
        finished = (StageState.DONE, StageState.SKIPPED)
        for m in videos:
            runs = [self.stage(m.id, st) for st in STAGE_ORDER]
            if any(r is not None and r.state is StageState.FAILED for r in runs):
                continue  # ruling R9: a failed file is finished; it is retried on rerun
            if any(r is None or r.state not in finished for r in runs):
                return "analysis"
        sync = self.stage(0, StageName.SYNC)
        if sync is None or sync.state not in (*finished, StageState.FAILED):
            return "analysis"
        return "analysis_done"


def _reasons_to_json(reasons: tuple[Reason, ...]) -> str:
    return json.dumps(
        [{"code": r.code, "ru": r.ru, "ky": r.ky} for r in reasons], ensure_ascii=False
    )


def _reasons_from_json(raw: str | None) -> tuple[Reason, ...]:
    if not raw:
        return ()
    return tuple(Reason(str(d["code"]), str(d["ru"]), str(d["ky"])) for d in json.loads(raw))


def _stage_row(r: tuple[object, ...]) -> StageRun:
    return StageRun(
        media_id=int(str(r[0])),
        stage=StageName(str(r[1])),
        state=StageState(str(r[2])),
        progress=float(str(r[3] or 0.0)),
        version=int(str(r[4] or 1)),
        error_code=None if r[5] is None else str(r[5]),
    )
