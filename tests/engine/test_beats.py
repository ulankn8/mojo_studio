import numpy as np

from toystudio.engine.audio.beats import CONFIDENT, beats_from_envelope

S = 1_000_000


def click_env(bpm: float, seconds: float, *, start_s: float = 0.0, rate: int = 100) -> np.ndarray:
    """100 Hz envelope of a click track: a sharp decaying hit on every beat."""
    n = int(seconds * rate)
    env = np.full(n, 0.01)
    t = start_s
    while t < seconds:
        k = int(t * rate)
        env[k : k + 8] += 0.8 * np.exp(-np.arange(min(8, n - k)) / 2.0)
        t += 60.0 / bpm
    return env


def test_click_track_tempo_phase_and_confidence():
    b = beats_from_envelope(click_env(120, 60, start_s=0.23))
    assert abs(b.tempo - 120) <= 2
    assert b.confidence >= CONFIDENT
    assert abs(b.period_us - 500_000) <= 10_000
    assert abs(b.beats_us[0] - 230_000) <= 20_000  # the grid sits on the clicks
    gaps = np.diff(b.beats_us)
    assert np.all(np.abs(gaps - 500_000) <= 15_000)


def test_slow_and_fast_tempi_stay_in_their_octave():
    assert abs(beats_from_envelope(click_env(98, 60)).tempo - 98) <= 2
    assert abs(beats_from_envelope(click_env(150, 60)).tempo - 150) <= 3


def test_noise_has_low_confidence():
    rng = np.random.default_rng(3)
    b = beats_from_envelope(0.1 + 0.05 * rng.random(6000))
    assert b.confidence < CONFIDENT


def test_too_short_or_silent():
    assert beats_from_envelope(np.zeros(5000)).tempo == 0.0
    assert beats_from_envelope(click_env(120, 2)).beats_us == ()


def test_speech_like_bursts_are_not_a_beat():
    rng = np.random.default_rng(5)
    env = 0.02 + 0.01 * rng.random(6000)
    pos = 0
    while pos < 6000:  # loud irregular bursts: talking, a crowd
        pos += int(rng.integers(20, 200))
        length = int(rng.integers(10, 150))
        env[pos : pos + length] += rng.uniform(0.1, 1.0)
        pos += length
    assert beats_from_envelope(env).confidence < CONFIDENT
