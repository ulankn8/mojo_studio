"""AI director request and answer handling, without the network (the API client is faked)."""

import base64
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from toystudio.cloud import director
from toystudio.cloud.director import DirectorError, build_content, plan_love_story
from toystudio.core.ai_plan import AiClip

S = 1_000_000
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def _clips(tmp_path, n: int = 3, frames: int = 2):
    out = []
    for i in range(n):
        paths = []
        for k in range(frames):
            p = tmp_path / f"ai{i}_{k}.jpg"
            p.write_bytes(JPEG)
            paths.append(p)
        flags = ("трясётся",) if i == 1 else ()
        out.append(AiClip(i + 1, round((5 + i) * S), flags, tuple(paths)))
    return out


class FakeClient:
    def __init__(self, answer=None, error=None):
        self.calls = []
        self.answer, self.error = answer, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self.parse))

    def parse(self, **kw):
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        return self.answer


def _answer(stop="end_turn", parsed=True):
    plan = director._Plan(
        story="Сборы, встреча, крыша",
        shots=[director._Shot(clip=2, start_pct=30, beats=3, part="intro", why="общий план")],
    )
    usage = SimpleNamespace(input_tokens=21000, output_tokens=7000)
    return SimpleNamespace(stop_reason=stop, parsed_output=plan if parsed else None,
                           model="claude-opus-5-5", usage=usage)  # fmt: skip


def test_content_has_task_captions_and_frames(tmp_path):
    content = build_content(_clips(tmp_path), tempo=98.0, beat_us=612_000, target_us=88 * S)
    assert (
        content[0]["type"] == "text"
        and "88 с" in content[0]["text"]
        and "98 BPM" in content[0]["text"]
    )
    captions = [b["text"] for b in content[1:] if b["type"] == "text"]
    assert captions == ["Клип 1 · 5.0 с", "Клип 2 · 6.0 с · трясётся", "Клип 3 · 7.0 с"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 6
    assert base64.standard_b64decode(images[0]["source"]["data"]) == JPEG
    assert images[0]["source"]["media_type"] == "image/jpeg"


def test_many_clips_get_one_frame_each(tmp_path):
    content = build_content(_clips(tmp_path, n=200), tempo=0.0, beat_us=600_000, target_us=88 * S)
    assert sum(b["type"] == "image" for b in content) == 200
    assert "без явного ритма" in content[0]["text"]


def test_plan_request_and_answer(tmp_path, monkeypatch):
    fake = FakeClient(answer=_answer())
    monkeypatch.setattr(director, "_client", lambda: fake)
    plan = plan_love_story(_clips(tmp_path), tempo=98.0, beat_us=612_000, target_us=88 * S)
    (call,) = fake.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["thinking"] == {"type": "adaptive"} and call["output_config"] == {"effort": "high"}
    assert call["fallbacks"] == "default" and call["output_format"] is director._Plan
    assert plan.story == "Сборы, встреча, крыша" and plan.shots[0].clip_id == 2
    assert plan.shots[0].part == "intro" and plan.clips_sent == 3
    assert abs(plan.cost_usd - (21000 * 4 + 7000 * 20) / 1e6) < 1e-9


def test_refusal_and_empty_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "_client", lambda: FakeClient(answer=_answer("refusal")))
    with pytest.raises(DirectorError) as e:
        plan_love_story(_clips(tmp_path), tempo=98.0, beat_us=612_000, target_us=88 * S)
    assert e.value.code == "refusal"
    monkeypatch.setattr(director, "_client", lambda: FakeClient(answer=_answer(parsed=False)))
    with pytest.raises(DirectorError) as e:
        plan_love_story(_clips(tmp_path), tempo=98.0, beat_us=612_000, target_us=88 * S)
    assert e.value.code == "empty"


def _status_error(cls, code):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(code, request=request)
    return cls("boom", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (lambda: _status_error(anthropic.AuthenticationError, 401), "bad_key"),
        (lambda: _status_error(anthropic.RateLimitError, 429), "network"),
        (lambda: _status_error(anthropic.InternalServerError, 500), "network"),
        (
            lambda: anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
            "network",
        ),
    ],
)
def test_api_errors_get_codes(tmp_path, monkeypatch, error, code):
    monkeypatch.setattr(director, "_client", lambda: FakeClient(error=error()))
    with pytest.raises(DirectorError) as e:
        plan_love_story(_clips(tmp_path), tempo=98.0, beat_us=612_000, target_us=88 * S)
    assert e.value.code == code


def test_no_key_and_no_clips(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no saved login profile either
    with pytest.raises(DirectorError) as e:
        plan_love_story(_clips(tmp_path), tempo=98.0, beat_us=612_000, target_us=88 * S)
    assert e.value.code == "no_key"
    with pytest.raises(DirectorError) as e:
        plan_love_story([], tempo=98.0, beat_us=612_000, target_us=88 * S)
    assert e.value.code == "empty"
