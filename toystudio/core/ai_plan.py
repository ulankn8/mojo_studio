"""The AI director's edit plan: what Claude is shown and what it answers (no network here)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Part = Literal["teaser", "intro", "development", "climax", "outro"]
OptionLabel = Literal["A", "B", "C"]
_PARTS: dict[str, Part] = {
    "teaser": "teaser",
    "intro": "intro",
    "development": "development",
    "climax": "climax",
    "outro": "outro",
}


def as_part(text: str) -> Part:
    """A shot's part by name; anything else is development."""
    return _PARTS.get(text, "development")


@dataclass(frozen=True)
class AiClip:
    """A clip as Claude sees it: its number, length, quality notes and a few frames."""

    id: int
    duration_us: int
    flags: tuple[str, ...]  # «трясётся», «размыто», «темно»
    frames: tuple[Path, ...]
    fps: float = 0.0


@dataclass(frozen=True)
class AiShot:
    clip_id: int
    start_pct: float  # where in the clip the shot starts, 0..100
    beats: int  # length in beats of the music
    part: Part
    why: str  # one short reason in Russian, shown in the draft
    slow: bool = False  # half speed (only footage with 50+ fps gets it)


@dataclass(frozen=True)
class AiPlan:
    story: str  # the idea of the clip in one or two sentences
    shots: tuple[AiShot, ...]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    clips_sent: int = 0
    revised: bool = False  # a built cut frozen back into a plan: its teaser and pace are in it

    @property
    def cost_usd(self) -> float:
        """Approximate price at Claude Opus 5.5 rates ($4 / $20 per million tokens)."""
        return (self.input_tokens * 4.0 + self.output_tokens * 20.0) / 1_000_000


# --- the AI's check of a finished cut: each shot with alternatives to swap a weak one for

ReviewPick = Literal["B", "C", "drop"]


@dataclass(frozen=True)
class ReviewOption:
    """A window of a clip a shot could show: "A" the shot as cut, "B" another moment of the
    same clip, "C" a clip of the love story the cut does not use."""

    label: OptionLabel
    clip_id: int
    start_pct: float  # where the window starts in its clip, 0..100 (as in AiShot)
    speed: float
    frame: Path


@dataclass(frozen=True)
class ReviewShot:
    index: int  # 1-based, as captioned for the AI
    at_us: int  # where the shot is in the cut
    beats: int
    part: Part
    why: str
    options: tuple[ReviewOption, ...]  # options[0] is "A"


@dataclass(frozen=True)
class ReviewChange:
    shot: int
    pick: ReviewPick
    problem: str  # in Russian, shown to the editor
    at_us: int = 0  # where the shot stood in the cut


@dataclass(frozen=True)
class AiReview:
    changes: tuple[ReviewChange, ...]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return (self.input_tokens * 4.0 + self.output_tokens * 20.0) / 1_000_000


# --- the shape of a cut, for music written to it

MusicMood = Literal["teaser", "intro", "calm", "tender", "pace", "climax", "outro"]


@dataclass(frozen=True)
class MusicSection:
    """A stretch of the cut with one atmosphere: what the music should do there."""

    start_us: int
    end_us: int
    mood: MusicMood
