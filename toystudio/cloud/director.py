"""AI director: Claude looks at frames of the love story clips and returns an edit plan.

Only small frames and short text leave the computer (about 84 tokens per 320x180 frame). The
key comes from the environment (ANTHROPIC_API_KEY) and is never stored by the program; later
it moves to the plan-service server (docs/architecture.md).
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field

from toystudio.core.ai_plan import AiClip, AiPlan, AiReview, AiShot, ReviewChange, ReviewShot

MODEL = "claude-opus-5-5"
MAX_FRAMES = 300  # per request; the API takes up to 600, more only adds cost
TIMEOUT_S = 300.0

SYSTEM = """You are the editor of a wedding video studio in Kyrgyzstan. You cut the love \
story of a wedding day into a same-day edit (SDE) that is shown at the restaurant in the \
evening. You see numbered clips from the love story shoot, each with up to three frames (from \
its beginning, middle and end), its length, frame rate and quality notes.

Build a clip that tells a short story: details and getting ready, the couple meeting, the \
walk and the most beautiful moments, a climax, and a strong final image. The studio's own \
films follow this shape:
- intro 5-10 s: an aerial or wide establishing shot;
- development: portraits, hands, details, the couple together, in an order that reads;
- climax at about 75-85 % of the length: 3-4 one-beat jump cuts from one take, then a few \
quick shots from other takes;
- outro 5-10 s: an aerial or the couple, calm.
Shots are 1-4 beats of the music long (median about 3 beats). Start each shot where its frames \
show the best moment (the frames are at 10, 50 and 90 % of the clip). Skip clips noted as \
shaky or blurry. Never put two near-identical shots side by side, use a clip at most twice, \
keep at most three shots in a row in one place, and alternate wide, medium and close shots.
Mark `slow` the emotional shots that gain from half speed: embraces, the veil, a look, a \
walk, hair or dress in the wind, a kiss - about a third of the shots, never in the climax. \
Only clips of 50 fps or more can be slowed; others play at real speed.

Write `story` and every `why` in Russian, short and concrete (what is in the shot and why it \
is there)."""


class DirectorError(Exception):
    """`code`: no_key | bad_key | network | refusal | empty."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class _Shot(BaseModel):
    clip: int = Field(description="Number of the clip as captioned")
    start_pct: float = Field(
        description="Where the shot starts in the clip, in percent from 0 to 100 (30 = 30 %)"
    )
    beats: int = Field(description="Length on screen in beats of the music, 1 to 8")
    slow: bool = Field(description="Half speed (only clips of 50+ fps)")
    part: Literal["intro", "development", "climax", "outro"]
    why: str


class _Plan(BaseModel):
    story: str
    shots: list[_Shot]


def _picked(clips: Sequence[AiClip]) -> tuple[list[AiClip], int]:
    """Clips to send and frames per clip (3, 2 or 1) within MAX_FRAMES (even spread when even
    one frame each is too many)."""
    chosen = list(clips)
    if len(chosen) > MAX_FRAMES:
        step = len(chosen) / MAX_FRAMES
        chosen = [chosen[int(i * step)] for i in range(MAX_FRAMES)]
    per_clip = next(n for n in (3, 2, 1)
                    if n == 1 or sum(min(len(c.frames), n) for c in chosen) <= MAX_FRAMES)  # fmt: skip
    return chosen, per_clip


def build_content(
    clips: Sequence[AiClip], *, tempo: float, beat_us: int, target_us: int
) -> list[dict[str, object]]:
    """The user message: the task in numbers, then each clip's caption and its frames."""
    chosen, per_clip = _picked(clips)
    beat_s = beat_us / 1_000_000
    tempo_text = f"{tempo:.0f} BPM" if tempo else "без явного ритма"
    content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": (
                f"Длина клипа: {target_us / 1_000_000:.0f} с. Музыка: {tempo_text}, "
                f"доля {beat_s:.2f} с — длины планов в долях. "
                f"Клипов: {len(chosen)}, по {per_clip} кадр(а) с каждого."
            ),
        }
    ]
    for c in chosen:
        notes = f" · {', '.join(c.flags)}" if c.flags else ""
        fps = f" · {c.fps:.0f} к/с" if c.fps else ""
        caption = f"Клип {c.id} · {c.duration_us / 1_000_000:.1f} с{fps}{notes}"
        content.append({"type": "text", "text": caption})
        for frame in c.frames[:per_clip]:
            data = base64.standard_b64encode(frame.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
                }
            )
    return content


def _client() -> anthropic.Anthropic:
    client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=2)
    # an API key, a token, or a saved `ant auth login` profile (credentials)
    if client.api_key is None and client.auth_token is None and client.credentials is None:
        raise DirectorError("no_key")
    return client


def _ask[T: BaseModel](
    system: str, content: list[dict[str, object]], output_format: type[T]
) -> tuple[T, Any]:
    """One structured request to Claude; API errors become DirectorError codes."""
    client = _client()
    try:
        response = client.beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            # on a policy decline the API reruns the request on a fallback model by itself
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": content}],  # type: ignore[misc,list-item]
            output_format=output_format,
        )
    except anthropic.AuthenticationError as e:
        raise DirectorError("bad_key", e.message) from None
    except anthropic.PermissionDeniedError as e:
        raise DirectorError("bad_key", e.message) from None
    except (anthropic.RateLimitError, anthropic.APIConnectionError) as e:
        raise DirectorError("network", str(e)) from None
    except anthropic.APIStatusError as e:
        raise DirectorError(
            "network" if e.status_code >= 500 else "bad_request", e.message
        ) from None
    if response.stop_reason == "refusal":
        raise DirectorError("refusal")
    parsed = response.parsed_output
    if parsed is None:
        raise DirectorError("empty", f"stop_reason={response.stop_reason}")
    return parsed, response


def plan_love_story(
    clips: Sequence[AiClip], *, tempo: float, beat_us: int, target_us: int
) -> AiPlan:
    """Ask Claude for the edit plan; raises DirectorError with a code the GUI can explain."""
    if not clips:
        raise DirectorError("empty")
    content = build_content(clips, tempo=tempo, beat_us=beat_us, target_us=target_us)
    parsed, response = _ask(SYSTEM, content, _Plan)
    shots = tuple(AiShot(s.clip, s.start_pct, s.beats, s.part, s.why, s.slow) for s in parsed.shots)
    chosen, _ = _picked(clips)
    return AiPlan(
        story=parsed.story,
        shots=shots,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        clips_sent=len(chosen),
    )


# --- the check of a finished cut

REVIEW_SYSTEM = """You check a finished wedding love-story clip before it is shown to the \
couple. For every shot you see its frame A as cut, and when there is one, B (another moment \
of the same take) and C (a take the clip does not use), all of the same length.

Replace A only when it is clearly weak: squinting or closed eyes, an awkward, stiff or \
unfinished pose, people still getting into position, a face hidden, cut off or turned away \
at the wrong moment, motion blur, an accidental look into the camera, an unflattering \
expression or mouth. Pick the better of B and C if one is clearly good, or "drop" when \
neither is. Leave good shots alone - most shots should stay. Keep the story: C must fit \
where the shot stands (part and description).

Write every `problem` in Russian, short and concrete (what is wrong in A)."""


class _Change(BaseModel):
    shot: int = Field(description="Number of the shot as captioned")
    pick: Literal["B", "C", "drop"]
    problem: str


class _Review(BaseModel):
    changes: list[_Change]


def review_content(shots: Sequence[ReviewShot]) -> list[dict[str, object]]:
    content: list[dict[str, object]] = [
        {"type": "text", "text": f"Планов в клипе: {len(shots)}. Варианты одинаковой длины."}
    ]
    for s in shots:
        if not s.options[0].frame.is_file():  # nothing to look at: the shot stays as it is
            continue
        content.append({"type": "text", "text": f"План {s.index} · {s.part} · {s.why}"})
        for o in s.options:
            if not o.frame.is_file():
                continue
            content.append({"type": "text", "text": o.label})
            data = base64.standard_b64encode(o.frame.read_bytes()).decode("ascii")
            content.append({"type": "image", "source": {"type": "base64",
                            "media_type": "image/jpeg", "data": data}})  # fmt: skip
    return content


def review_cut(shots: Sequence[ReviewShot]) -> AiReview:
    """Ask Claude which shots of the cut to swap; raises DirectorError like plan_love_story."""
    if not shots:
        raise DirectorError("empty")
    parsed, response = _ask(REVIEW_SYSTEM, review_content(shots), _Review)
    at = {s.index: s.at_us for s in shots}
    changes = tuple(ReviewChange(c.shot, c.pick, c.problem, at[c.shot]) for c in parsed.changes
                    if c.shot in at)  # fmt: skip
    return AiReview(changes, response.model, response.usage.input_tokens,
                    response.usage.output_tokens)  # fmt: skip
