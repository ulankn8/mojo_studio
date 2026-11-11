"""AI music: the prompt follows the clip's shape; answers and errors become codes, no network."""

import base64
from types import SimpleNamespace

import pytest
from google.genai import errors

from toystudio.cloud import composer
from toystudio.cloud.composer import MODEL, ComposerError, compose, music_prompt

MP3 = b"ID3-fake-mp3"


class FakeClient:
    def __init__(self, answer=None, error=None):
        self.calls = []
        self.answer, self.error = answer, error
        self.interactions = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        return self.answer


def _answer(data=MP3, status="completed"):
    audio = SimpleNamespace(data=base64.b64encode(data).decode()) if data else None
    return SimpleNamespace(output_audio=audio, output_text="Intro, build, climax", status=status)


def test_prompt_has_the_clip_shape_tempo_and_no_vocals():
    text = music_prompt(88, "romantic", 92)
    assert "Instrumental only, no vocals" in text and "92 BPM" in text
    assert "exactly 1:28" in text and "piano" in text
    assert "[1:06 - 1:15] Climax" in text  # 75-85 % of 88 s, where the edit's climax is
    assert text.rstrip().endswith("no fade-out")
    assert "3:00" in music_prompt(180, "light", 124) and "acoustic guitar" in music_prompt(
        180, "light", 124
    )


def test_answer_becomes_mp3_bytes(monkeypatch):
    fake = FakeClient(answer=_answer())
    monkeypatch.setattr(composer, "_client", lambda: fake)
    music = compose(88, mood="epic", bpm=92)
    (call,) = fake.calls
    assert call["model"] == MODEL and call["response_format"] == {"type": "audio"}
    assert "orchestra" in call["input"]
    assert music.audio == MP3 and music.notes == "Intro, build, climax" and music.cost_usd > 0


def _next_gen(status):
    """An error of the interactions API (its own class, with an HTTP status)."""
    from google.genai._gaos.lib.compat_errors import GeminiNextGenAPIClientError

    err = GeminiNextGenAPIClientError("boom")
    err.status_code = status
    return err


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (errors.ClientError(401, {"error": {"message": "bad key"}}), "bad_key"),
        (errors.ClientError(403, {"error": {"message": "billing"}}), "bad_key"),
        (errors.ClientError(429, {"error": {"message": "slow down"}}), "network"),
        (errors.ClientError(400, {"error": {"message": "nope"}}), "bad_request"),
        (errors.ServerError(503, {"error": {"message": "busy"}}), "network"),
        (_next_gen(404), "bad_request"),
        (_next_gen(429), "network"),
        (ConnectionError("offline"), "network"),
    ],
)
def test_errors_become_codes(monkeypatch, error, code):
    monkeypatch.setattr(composer, "_client", lambda: FakeClient(error=error))
    with pytest.raises(ComposerError) as e:
        compose(88)
    assert e.value.code == code


def test_empty_or_failed_answers(monkeypatch):
    monkeypatch.setattr(composer, "_client", lambda: FakeClient(answer=_answer(data=b"")))
    with pytest.raises(ComposerError) as e:
        compose(88)
    assert e.value.code == "empty"
    monkeypatch.setattr(composer, "_client",
                        lambda: FakeClient(answer=_answer(data=b"", status="failed")))  # fmt: skip
    with pytest.raises(ComposerError) as e:
        compose(88)
    assert e.value.code == "refusal"


def test_no_key_is_explained(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ComposerError) as e:
        compose(88)
    assert e.value.code == "no_key"


def test_prompt_for_a_cut_follows_its_atmosphere():
    from toystudio.cloud.composer import prompt_for_cut
    from toystudio.core.ai_plan import MusicSection

    cut = [
        MusicSection(0, 4_200_000, "teaser"),
        MusicSection(4_200_000, 12_000_000, "intro"),
        MusicSection(12_000_000, 40_000_000, "calm"),
        MusicSection(40_000_000, 52_000_000, "pace"),
        MusicSection(52_000_000, 66_000_000, "tender"),
        MusicSection(66_000_000, 80_000_000, "climax"),
        MusicSection(80_000_000, 88_000_000, "outro"),
    ]
    text = prompt_for_cut(cut, "romantic", 92)
    assert "[0:00 - 0:04] Teaser: bright and energetic" in text
    assert "[0:40 - 0:52] Lively: rhythm and light percussion" in text
    assert "[1:06 - 1:20] Climax" in text and "[1:28 - 1:31] Ending" in text
    assert "Total length 1:31" in text and "92 BPM" in text and "no vocals" in text


def test_compose_sends_the_cut_prompt_when_given_sections(monkeypatch):
    from toystudio.core.ai_plan import MusicSection

    fake = FakeClient(answer=_answer())
    monkeypatch.setattr(composer, "_client", lambda: fake)
    compose(91, sections=[MusicSection(0, 88_000_000, "calm")])
    assert "[0:00 - 1:28] Calm" in fake.calls[0]["input"]
