"""Tempo and beat grid of a music track from its loudness envelope (no extra models).

Onset strength is the positive change of the log envelope; the tempo is the autocorrelation
peak between 60 and 180 BPM (weighted towards ~120 BPM against octave errors); the phase is
the offset whose grid collects the most onset strength. Confidence is the normalised
autocorrelation at the beat period scaled by AC_FULL: on real wedding music it is 0.26-0.28
(confidence ~0.9), on speech, crowd noise or white noise 0.03-0.04 (~0.1), a click track ~1.
Cuts are put on beats only when confidence >= CONFIDENT (rule 24 of the wedding analysis).

Music written to order (the AI's) comes with the tempo it was asked for: then only tempi
within HINT_SPREAD of it are considered, and a modest peak there (HINT_MIN_AC) is enough to
trust it - a piano piece has soft onsets, but it was written to that beat.
"""

from dataclasses import dataclass

import numpy as np

from toystudio.core.models import US_PER_S

ENV_HZ = 100
MIN_BPM, MAX_BPM = 60.0, 180.0
CONFIDENT = 0.6
AC_FULL = 0.3  # normalised autocorrelation that counts as a sure beat
HINT_SPREAD = 0.1
HINT_MIN_AC = 0.05


@dataclass(frozen=True)
class Beats:
    tempo: float  # beats per minute; 0 when the track is too short or silent
    beats_us: tuple[int, ...]
    confidence: float  # 0..1

    @property
    def period_us(self) -> int:
        return round(60 * US_PER_S / self.tempo) if self.tempo else 0


def onset_strength(env: np.ndarray) -> np.ndarray:
    x = np.log1p(100.0 * np.asarray(env, dtype=np.float64))
    d = np.diff(x, prepend=x[:1])
    d[d < 0] = 0.0
    return d - d.mean() if len(d) else d


def beats_from_envelope(
    env: np.ndarray, rate: int = ENV_HZ, tempo_hint: float | None = None
) -> Beats:
    o = onset_strength(env)
    lo, hi = int(rate * 60 / MAX_BPM), int(np.ceil(rate * 60 / MIN_BPM))
    if tempo_hint:
        lo = max(lo, int(rate * 60 / (tempo_hint * (1 + HINT_SPREAD))))
        hi = min(hi, int(np.ceil(rate * 60 / (tempo_hint * (1 - HINT_SPREAD)))))
    if len(o) < 4 * hi or not np.any(o):
        return Beats(0.0, (), 0.0)
    n = len(o)
    spec = np.fft.rfft(o, 2 * n)
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    if ac[0] <= 0:
        return Beats(0.0, (), 0.0)
    ac = ac / ac[0]
    lags = np.arange(lo, hi + 1)
    bpm = 60.0 * rate / lags
    weight = (
        np.ones_like(bpm)
        if tempo_hint
        else np.exp(-0.5 * (np.log2(bpm / 120.0) / 0.9) ** 2)  # prefer ~120 against octaves
    )
    best = int(lags[np.argmax(ac[lags] * weight)])
    period = float(best)
    if lo < best < hi:  # parabolic refinement of the peak
        a, b, c = ac[best - 1], ac[best], ac[best + 1]
        denom = a - 2 * b + c
        if denom < 0:
            period = best + 0.5 * (a - c) / denom
    confidence = float(np.clip(ac[best] / AC_FULL, 0.0, 1.0))
    if tempo_hint and ac[best] >= HINT_MIN_AC:
        confidence = max(confidence, CONFIDENT)
    steps = np.arange(0.0, n - 1, period)
    phases = np.arange(0, int(np.ceil(period)))
    scores = [o[np.clip(np.round(steps + p).astype(int), 0, n - 1)].sum() for p in phases]
    phase = int(phases[int(np.argmax(scores))])
    grid = np.arange(phase, n, period)
    beats = tuple(round(t * US_PER_S / rate) for t in grid)
    return Beats(60.0 * rate / period, beats, confidence)
