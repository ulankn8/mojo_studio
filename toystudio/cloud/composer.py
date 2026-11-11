"""AI music for a clip: Google Lyria writes an instrumental piece shaped like the edit.

The clip's structure is fixed (intro, development, a climax at 75-85 %, a calm outro), so the
prompt gives Lyria the same structure with timestamps, a tempo and a mood; the edit is then
cut to that music's beats like to any track of the library. The key comes from the
environment (GEMINI_API_KEY or GOOGLE_API_KEY) and is never stored by the program.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from google import genai
from google.genai import errors

from toystudio.core.ai_plan import MusicSection

try:  # the interactions API raises its own errors, from a private module of the SDK
    from google.genai._gaos.lib.compat_errors import GeminiNextGenAPIClientError
except ImportError:  # pragma: no cover - an SDK that moved them: the public base still works
    GeminiNextGenAPIClientError = errors.APIError  # type: ignore[assignment,misc]

MODEL = "lyria-3.5"
PRICE_USD = 0.08  # a full song (Gemini API pricing)
TIMEOUT_MS = 300_000
Mood = Literal["romantic", "light", "epic"]
MOODS: dict[str, str] = {
    "romantic": "cinematic and romantic: felt piano, warm strings, soft pads, tender and "
    "hopeful, like a wedding film",
    "light": "light and bright: acoustic guitar, ukulele, light claps and glockenspiel, "
    "sunny and joyful",
    "epic": "epic and emotional: orchestra with big strings, piano and cinematic percussion, "
    "a powerful build",
}


class ComposerError(Exception):
    """`code`: no_key | bad_key | network | bad_request | refusal | empty."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class AiMusic:
    audio: bytes  # MP3, 44.1 kHz stereo
    notes: str = ""  # the structure Lyria describes
    model: str = MODEL
    cost_usd: float = PRICE_USD


def _ts(seconds: float) -> str:
    s = round(seconds)
    return f"{s // 60}:{s % 60:02d}"


def music_prompt(seconds: float, mood: str, bpm: int) -> str:
    """The prompt: instrumental, the mood, the tempo, and the clip's shape in timestamps."""
    intro, climax, outro = min(8.0, seconds * 0.1), seconds * 0.75, seconds * 0.85
    parts = [
        (0, intro, "Intro: soft and sparse, the theme appears"),
        (intro, seconds * 0.45, "Verse: the theme grows, gentle movement"),
        (seconds * 0.45, climax, "Build: fuller, rising, more energy"),
        (climax, outro, "Climax: the fullest, most emotional moment, strong accents"),
        (outro, seconds, "Outro: calm, the theme once more, a clear final chord, no fade-out"),
    ]
    lines = "\n".join(f"[{_ts(a)} - {_ts(b)}] {text}" for a, b, text in parts)
    return (
        f"Instrumental only, no vocals. {MOODS.get(mood, MOODS['romantic'])}. "
        f"Tempo {bpm} BPM, steady beat. Total length exactly {_ts(seconds)}, ending cleanly "
        f"on the last chord at {_ts(seconds)}.\n{lines}"
    )


# what the music does in each stretch of the cut
SECTION_TEXT: dict[str, str] = {
    "teaser": "Teaser: bright and energetic, quick accents on every beat, a short burst",
    "intro": "Intro: soft and sparse, the theme appears",
    "calm": "Calm: gentle, flowing, the theme develops",
    "tender": "Tender: intimate, softer, slow emotional phrase",
    "pace": "Lively: rhythm and light percussion come in, more movement",
    "climax": "Climax: the fullest, most emotional moment, strong accents",
    "outro": "Outro: calm, the theme once more, settling",
}


def prompt_for_cut(sections: Sequence[MusicSection], mood: str, bpm: int) -> str:
    """The prompt for music written to a finished cut: its stretches with their atmosphere,
    in timestamps, then a clear final chord (with 3 s to ring out)."""
    end = sections[-1].end_us / 1_000_000 if sections else 0.0
    lines = [f"[{_ts(s.start_us / 1_000_000)} - {_ts(s.end_us / 1_000_000)}] {SECTION_TEXT[s.mood]}"
             for s in sections]  # fmt: skip
    lines.append(
        f"[{_ts(end)} - {_ts(end + 3)}] Ending: a clear final chord rings out, no fade-out"
    )
    return (
        f"Instrumental only, no vocals. {MOODS.get(mood, MOODS['romantic'])}. "
        f"Tempo {bpm} BPM, steady beat. The music follows a wedding film's edit: change "
        f"exactly at these timestamps. Total length {_ts(end + 3)}.\n" + "\n".join(lines)
    )


def _client() -> genai.Client:
    try:
        # Lyria is served by the v1beta API (v1 answers "model not found")
        return genai.Client(http_options={"api_version": "v1beta", "timeout": TIMEOUT_MS})
    except ValueError as e:  # no key in the environment
        raise ComposerError("no_key", str(e)) from None


def _code(e: Exception) -> str:
    """An SDK error as a code: by its HTTP status, a connection problem when it has none."""
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    if not isinstance(status, int):
        return "network"
    if status in (401, 403):
        return "bad_key"
    if status == 429 or status >= 500:
        return "network"
    return "bad_request"


def compose(
    seconds: float,
    *,
    mood: str = "romantic",
    bpm: int = 92,
    sections: Sequence[MusicSection] = (),
) -> AiMusic:
    """Ask Lyria for the clip's music - shaped like a clip in general, or, given `sections` of
    a finished cut, following its atmosphere; raises ComposerError with a code the GUI can
    explain."""
    client = _client()
    prompt = prompt_for_cut(sections, mood, bpm) if sections else music_prompt(seconds, mood, bpm)
    try:
        interaction = client.interactions.create(
            model=MODEL,
            input=prompt,
            response_format={"type": "audio"},
        )
    except (errors.APIError, GeminiNextGenAPIClientError, OSError) as e:
        raise ComposerError(_code(e), str(e)[:500]) from None
    audio = getattr(interaction, "output_audio", None)
    if audio is None or not getattr(audio, "data", None):
        status = getattr(interaction, "status", "")
        raise ComposerError("refusal" if status == "failed" else "empty", str(status))
    data = base64.b64decode(audio.data)  # the SDK keeps it base64
    return AiMusic(data, getattr(interaction, "output_text", "") or "")
