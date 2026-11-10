"""Core domain types."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

US_PER_S = 1_000_000


class MomentType(StrEnum):
    TOAST = "toast"
    DANCE = "dance"
    ENTRANCE = "entrance"
    GIFTS = "gifts"
    APPLAUSE = "applause"
    MUSIC = "music"


class Mark(StrEnum):
    NONE = "none"
    MUST = "must"
    EXCLUDE = "exclude"


class ToiKind(StrEnum):
    WEDDING = "wedding"
    TOI = "toi"
    JUBILEE = "jubilee"
    BESH_BARMAK = "besh_barmak"
    SUNNOT = "sunnot"
    KYZ_UZATUU = "kyz_uzatuu"
    OTHER = "other"


class Track(StrEnum):
    VIDEO = "video"
    MUSIC = "music"
    TITLE = "title"


class AudioMode(StrEnum):
    ORIGINAL = "original"
    MUSIC = "music"
    DUCK = "duck"


@dataclass(frozen=True)
class Reason:
    code: str
    ru: str
    ky: str


@dataclass(frozen=True)
class Moment:
    id: int
    media_id: int
    type: MomentType
    start: int
    end: int
    score: float
    mark: Mark = Mark.NONE

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"Moment end ({self.end}) must be greater than start ({self.start})")

    @property
    def duration(self) -> int:
        return self.end - self.start


class MediaKind(StrEnum):
    """Container of a file: video, or audio only (stored as "music" for schema compatibility)."""

    VIDEO = "video"
    MUSIC = "music"


class MediaRole(StrEnum):
    """What a file is for in the edit: its track."""

    PRE = "pre"  # mixed shooting without a main camera: before the restaurant, love story, ad
    MAIN = "main"  # V1: main camera, the story follows it
    CUTAWAY = "cutaway"  # V2: second camera, inserts over V1
    SPEECH = "speech"  # clean speech from the sound desk or mics, synced to V1
    MUSIC = "music"  # music library

    @property
    def kind(self) -> MediaKind:
        return MediaKind.MUSIC if self in (MediaRole.SPEECH, MediaRole.MUSIC) else MediaKind.VIDEO

    @staticmethod
    def default_for(kind: MediaKind) -> "MediaRole":
        """Role of a file stored before roles existed."""
        return MediaRole.MAIN if kind is MediaKind.VIDEO else MediaRole.MUSIC


class ProjectFormat(StrEnum):
    """What is being edited; decides which tracks the files go to."""

    WEDDING_EXPRESS = "wedding_express"
    WEDDING_FULL = "wedding_full"
    LOVE_STORY = "love_story"
    PODCAST = "podcast"
    AD = "ad"
    TOI = (
        "toi"  # toi, jubilee and other events: one video zone plus music (projects before formats)
    )


FORMAT_ROLES: dict[ProjectFormat, tuple[MediaRole, ...]] = {
    ProjectFormat.WEDDING_EXPRESS: (MediaRole.PRE, MediaRole.MUSIC),
    ProjectFormat.WEDDING_FULL: (
        MediaRole.PRE,
        MediaRole.MAIN,
        MediaRole.CUTAWAY,
        MediaRole.SPEECH,
        MediaRole.MUSIC,
    ),
    ProjectFormat.LOVE_STORY: (MediaRole.PRE, MediaRole.MUSIC),
    ProjectFormat.PODCAST: (MediaRole.MAIN, MediaRole.CUTAWAY, MediaRole.SPEECH, MediaRole.MUSIC),
    ProjectFormat.AD: (MediaRole.PRE, MediaRole.MUSIC),
    ProjectFormat.TOI: (MediaRole.MAIN, MediaRole.MUSIC),
}
SYNCED_ROLES = (MediaRole.MAIN, MediaRole.CUTAWAY, MediaRole.SPEECH)


def format_toi_kind(fmt: ProjectFormat, chosen: ToiKind) -> ToiKind:
    """Toi kind stored for a project: weddings are weddings, the toi format keeps the choice."""
    if fmt is ProjectFormat.TOI:
        return chosen
    if fmt in (ProjectFormat.WEDDING_EXPRESS, ProjectFormat.WEDDING_FULL):
        return ToiKind.WEDDING
    return ToiKind.OTHER


class MediaStatus(StrEnum):
    OK = "ok"
    CORRUPT = "corrupt"
    MISSING = "missing"


@dataclass(frozen=True)
class MediaInfo:
    duration_us: int
    width: int  # display size, after rotation
    height: int
    rotation: int
    fps: float
    codec: str
    hdr: bool
    has_audio: bool
    has_video: bool
    bit_rate: int
    creation_time: str | None


class StageName(StrEnum):
    PROBE = "probe"
    PROXY = "proxy"
    SPEECH = "speech"
    SCENES = "scenes"
    QUALITY = "quality"
    MOMENTS = "moments"
    SYNC = "sync"  # project-level stage, stored with media_id = 0


STAGE_ORDER: tuple[StageName, ...] = (
    StageName.PROBE,
    StageName.PROXY,
    StageName.SPEECH,
    StageName.SCENES,
    StageName.QUALITY,
    StageName.MOMENTS,
)
PROJECT_STAGES: tuple[StageName, ...] = (StageName.SYNC,)


class StageState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class StageRun:
    media_id: int
    stage: StageName
    state: StageState
    progress: float
    version: int
    error_code: str | None


class QualityLabel(StrEnum):
    CLEAR = "clear"
    DARK = "dark"
    SHAKY = "shaky"
    BLURRY = "blurry"


@dataclass(frozen=True)
class QualityInterval:
    media_id: int
    start_us: int
    end_us: int
    label: QualityLabel


@dataclass(frozen=True)
class MediaRow:
    id: int
    kind: MediaKind
    path: Path
    copied: bool
    size: int
    info: MediaInfo | None
    status: MediaStatus
    sync_offset_us: int | None
    sync_confidence: float | None
    role: MediaRole

    @property
    def analyzable(self) -> bool:
        """Videos of every role and speech recordings go through analysis; music does not."""
        return self.kind is MediaKind.VIDEO or self.role is MediaRole.SPEECH


@dataclass(frozen=True)
class SyncResult:
    media_id: int
    offset_us: int | None
    confidence: float


@dataclass(frozen=True)
class SpeechRegion:
    start_us: int
    end_us: int
    prob: float  # mean speech probability over the region


class AudioLabel(StrEnum):
    SPEECH = "speech"
    APPLAUSE = "applause"
    MUSIC = "music"
    CHEER = "cheer"
    LAUGHTER = "laughter"


@dataclass(frozen=True)
class AudioEvent:
    media_id: int
    start_us: int
    end_us: int
    label: AudioLabel
    score: float  # mean label score over the event


@dataclass(frozen=True)
class MomentRow:
    """A detected moment as stored in the project database."""

    id: int
    media_id: int
    type: MomentType
    start_us: int
    end_us: int
    score: float
    quality_label: QualityLabel
    reasons: tuple[Reason, ...] = ()
    mark: Mark = Mark.NONE
    group_id: int | None = None
    preview: str | None = None
