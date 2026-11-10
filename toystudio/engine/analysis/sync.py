"""Camera sync: time offsets of files against a reference by audio envelope."""

import math

import numpy as np

from toystudio.core.models import US_PER_S, SyncResult

_GUARD_S = 0.5


def _prepare(env: np.ndarray, rate_hz: int) -> np.ndarray:
    x = np.log1p(np.asarray(env, dtype=np.float64))
    n = len(x)
    win = max(1, rate_hz)
    csum = np.concatenate([[0.0], np.cumsum(x)])
    idx = np.arange(n)
    lo = np.clip(idx - win // 2, 0, n)
    hi = np.clip(idx + win - win // 2, 0, n)
    mean = (csum[hi] - csum[lo]) / np.maximum(hi - lo, 1)
    x = x - mean
    std = x.std()
    return x / std if std > 0 else x


def estimate_offset(ref: np.ndarray, other: np.ndarray, rate_hz: int = 100) -> tuple[int, float]:
    """Return (offset_us, confidence): where `other` starts on the `ref` timeline."""
    a = _prepare(ref, rate_hz)
    b = _prepare(other, rate_hz)
    n_ref, n_other = len(a), len(b)
    if n_ref == 0 or n_other == 0:
        return 0, 0.0
    size = 1 << (n_ref + n_other - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    # lags from -(n_other-1) to n_ref-1
    lags = np.concatenate([corr[size - (n_other - 1) :], corr[:n_ref]])
    first_lag = -(n_other - 1)
    peak = int(np.argmax(lags))
    guard = int(_GUARD_S * rate_hz)
    mask = np.ones(len(lags), dtype=bool)
    mask[max(0, peak - guard) : peak + guard + 1] = False
    second = float(lags[mask].max()) if mask.any() else float(lags.mean())
    std = float(lags.std())
    confidence = (float(lags[peak]) - second) / std if std > 0 else 0.0
    offset_us = round((peak + first_lag) * US_PER_S / rate_hz)
    return offset_us, confidence


def sync_to_reference(
    ref: np.ndarray,
    envelopes: dict[int, np.ndarray | None],
    rate_hz: int = 100,
    min_confidence: float = 6.0,
    min_overlap_s: float = 30.0,
) -> list[SyncResult]:
    """Offset of every file on the `ref` timeline; None when unsure or without audio."""
    results: list[SyncResult] = []
    for mid, env in sorted(envelopes.items()):
        if env is None:
            results.append(SyncResult(mid, None, 0.0))
            continue
        offset_us, conf = estimate_offset(ref, env, rate_hz)
        start = round(offset_us * rate_hz / US_PER_S)
        overlap_s = (min(len(ref), start + len(env)) - max(0, start)) / rate_hz
        ok = conf >= min_confidence and overlap_s >= min_overlap_s
        results.append(SyncResult(mid, offset_us if ok else None, conf))
    return results


def sync_project(
    envelopes: dict[int, np.ndarray | None],
    rate_hz: int = 100,
    min_confidence: float = 6.0,
    min_overlap_s: float = 30.0,
) -> list[SyncResult]:
    """All files on the timeline of the one with the longest audio (offset 0)."""
    audio = {mid: env for mid, env in envelopes.items() if env is not None}
    if not audio:
        return [SyncResult(mid, None, 0.0) for mid in sorted(envelopes)]
    ref_id = min(audio, key=lambda m: (-len(audio[m]), m))
    others = {mid: env for mid, env in envelopes.items() if mid != ref_id}
    found = sync_to_reference(audio[ref_id], others, rate_hz, min_confidence, min_overlap_s)
    return sorted([SyncResult(ref_id, 0, math.inf), *found], key=lambda r: r.media_id)


def sync_tracks(
    main: dict[int, np.ndarray | None],
    cutaway: dict[int, np.ndarray | None],
    speech: list[tuple[int, np.ndarray | None]],
    rate_hz: int = 100,
    min_confidence: float = 6.0,
    min_overlap_s: float = 30.0,
) -> list[SyncResult]:
    """Restaurant / podcast: V1 and V2 cameras on one timeline.

    The reference is the clean speech recording. A recorder splits one long take into files
    (e.g. every 2 GB), so `speech` comes in recording order and is laid back to back. Without
    speech the longest main camera is the reference (offset 0); cameras are V1 first, then V2.
    """
    args = (rate_hz, min_confidence, min_overlap_s)
    parts = [(mid, env) for mid, env in speech if env is not None and len(env)]
    if parts:
        starts: dict[int, int] = {}
        pos = 0
        for mid, env in parts:
            starts[mid] = pos
            pos += len(env)
        ref = np.concatenate([env for _, env in parts])
        results = [
            SyncResult(m, round(at * US_PER_S / rate_hz), math.inf) for m, at in starts.items()
        ]
        results += [SyncResult(mid, None, 0.0) for mid, _ in speech if mid not in starts]
        results += sync_to_reference(ref, {**main, **cutaway}, *args)
        return sorted(results, key=lambda r: r.media_id)
    unplaced = [SyncResult(mid, None, 0.0) for mid, _ in speech]
    pool = {mid: env for mid, env in main.items() if env is not None and len(env)}
    if not pool:
        pool = {mid: env for mid, env in cutaway.items() if env is not None and len(env)}
    if not pool:
        return sorted(unplaced + sync_to_reference(np.empty(0), {**main, **cutaway}, *args),
                      key=lambda r: r.media_id)  # fmt: skip
    ref_id = min(pool, key=lambda m: (-len(pool[m]), m))
    others = {mid: env for mid, env in {**main, **cutaway}.items() if mid != ref_id}
    results = [
        SyncResult(ref_id, 0, math.inf),
        *unplaced,
        *sync_to_reference(pool[ref_id], others, *args),
    ]
    return sorted(results, key=lambda r: r.media_id)
