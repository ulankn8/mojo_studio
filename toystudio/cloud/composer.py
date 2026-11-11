"""AI music for a clip: Google Lyria writes an instrumental piece shaped like the edit.

The clip's structure is fixed (intro, development, a climax at 75-85 %, a calm outro), so the
prompt gives Lyria the same structure with timestamps, a tempo and a mood; the edit is then
cut to that music's beats like to any track of the library. The key comes from the
environment (GEMINI_API_KEY or GOOGLE_API_KEY) and is never stored by the program.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal

from google import genai
from google.genai import errors

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


def compose(seconds: float, *, mood: str = "romantic", bpm: int = 92) -> AiMusic:
    """Ask Lyria for the clip's music; raises ComposerError with a code the GUI can explain."""
    client = _client()
    try:
        interaction = client.interactions.create(
            model=MODEL,
            input=music_prompt(seconds, mood, bpm),
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
