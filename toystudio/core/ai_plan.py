"""The AI director's edit plan: what Claude is shown and what it answers (no network here)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Part = Literal["intro", "development", "climax", "outro"]


@dataclass(frozen=True)
class AiClip:
    """A clip as Claude sees it: its number, length, quality notes and a few frames."""

    id: int
    duration_us: int
    flags: tuple[str, ...]  # «трясётся», «размыто», «темно»
    frames: tuple[Path, ...]


@dataclass(frozen=True)
class AiShot:
    clip_id: int
    start_pct: float  # where in the clip the shot starts, 0..100
    beats: int  # length in beats of the music
    part: Part
    why: str  # one short reason in Russian, shown in the draft


@dataclass(frozen=True)
class AiPlan:
    story: str  # the idea of the clip in one or two sentences
    shots: tuple[AiShot, ...]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    clips_sent: int = 0

    @property
    def cost_usd(self) -> float:
        """Approximate price at Claude Opus 5.5 rates ($4 / $20 per million tokens)."""
        return (self.input_tokens * 4.0 + self.output_tokens * 20.0) / 1_000_000
