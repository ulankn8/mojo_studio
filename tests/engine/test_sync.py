import math

import numpy as np

from toystudio.engine.analysis.sync import estimate_offset, sync_project, sync_tracks


def noise_env(n: int, seed: int) -> np.ndarray:
    """Deterministic 100 Hz envelope of 'speech and music': bursts over a low floor."""
    rng = np.random.default_rng(seed)
    env = 0.02 + 0.01 * rng.random(n)
    pos = 0
    while pos < n:
        pos += int(rng.integers(20, 200))
        length = min(int(rng.integers(10, 150)), n - pos)
        if length <= 0:
            break
        env[pos : pos + length] += rng.uniform(0.1, 1.0) * (0.5 + rng.random(length))
        pos += length
    return env.astype(np.float32)


def test_offset_found_within_10ms():
    base = noise_env(60_00, 1)  # 60 s
    other = base[1234 : 1234 + 30_00]  # piece from 12.34 s, 30 s long
    off, conf = estimate_offset(base, other)
    assert abs(off - 12_340_000) <= 10_000 and conf >= 6.0


def test_unrelated_audio_low_confidence():
    _off, conf = estimate_offset(noise_env(6000, 1), noise_env(3000, 2))
    assert conf < 6.0


def test_same_seed_is_deterministic_and_seeds_differ():
    assert np.array_equal(noise_env(500, 3), noise_env(500, 3))
    assert not np.array_equal(noise_env(500, 3), noise_env(500, 4))


def test_negative_offset():
    base = noise_env(6000, 1)
    other = np.concatenate([noise_env(500, 9), base[:3000]])  # starts 5 s before ref
    off, conf = estimate_offset(base, other)
    assert abs(off - (-5_000_000)) <= 10_000 and conf >= 6.0


def test_sync_skips_media_without_audio():
    res = {
        r.media_id: r
        for r in sync_project({1: noise_env(6000, 1), 2: noise_env(6000, 1)[500:3800], 3: None})
    }
    assert res[1].offset_us == 0 and res[1].confidence == math.inf
    assert abs(res[2].offset_us - 5_000_000) <= 10_000
    assert res[3].offset_us is None and res[3].confidence == 0.0


def test_short_overlap_rejected():
    base = noise_env(6000, 1)
    other = base[1000:2000]  # 10 s overlap, well matched
    res = {r.media_id: r for r in sync_project({1: base, 2: other})}
    assert res[2].offset_us is None
    assert res[2].confidence >= 6.0


def test_other_camera_noise_and_gain():
    base = noise_env(6000, 5)
    piece = base[2000:5000]
    rng = np.random.default_rng(11)
    other = 0.3 * piece + rng.normal(0.0, 0.2 * float(piece.std()), piece.shape)
    off, conf = estimate_offset(base, other.astype(np.float32))
    assert abs(off - 20_000_000) <= 20_000 and conf >= 6.0


def test_reference_is_longest_ties_lowest_id():
    res = {r.media_id: r for r in sync_project({5: noise_env(6000, 1), 2: noise_env(6000, 1)})}
    assert res[2].offset_us == 0 and res[2].confidence == math.inf
    assert res[5].offset_us == 0


# --- sync by tracks: restaurant / podcast ---


def _by_id(results):
    return {r.media_id: r for r in results}


def test_tracks_cameras_on_speech_recording_split_in_files():
    take = noise_env(120_00, 5)  # 120 s recorder take, split at 70 s into two files
    speech = [(10, take[:70_00]), (11, take[70_00:])]
    main = {1: take[80_00:115_00]}  # V1 clip 80..115 s: inside the second file
    cutaway = {2: take[20_00:60_00]}  # V2 clip 20..60 s: inside the first file
    r = _by_id(sync_tracks(main, cutaway, speech))
    assert r[10].offset_us == 0 and r[11].offset_us == 70_000_000
    assert abs(r[1].offset_us - 80_000_000) <= 10_000 and r[1].confidence >= 6.0
    assert abs(r[2].offset_us - 20_000_000) <= 10_000


def test_tracks_without_speech_longest_main_is_reference():
    base = noise_env(90_00, 6)
    main = {1: base[20_00:70_00], 3: base[30_00:65_00]}  # longest main: 50 s from 20 s
    cutaway = {2: base[:90_00]}  # the longest file overall, but a cutaway never leads
    r = _by_id(sync_tracks(main, cutaway, []))
    assert r[1].offset_us == 0 and math.isinf(r[1].confidence)
    assert abs(r[3].offset_us - 10_000_000) <= 10_000
    assert abs(r[2].offset_us + 20_000_000) <= 10_000  # starts 20 s before the reference


def test_tracks_unrelated_camera_gets_no_offset():
    take = noise_env(100_00, 7)
    r = _by_id(sync_tracks({1: noise_env(40_00, 99)}, {}, [(10, take)]))
    assert r[1].offset_us is None and r[1].confidence < 6.0
    assert r[10].offset_us == 0


def test_tracks_camera_without_audio_and_empty_speech_file():
    take = noise_env(80_00, 8)
    r = _by_id(sync_tracks({1: None, 2: take[10_00:60_00]}, {}, [(10, None), (11, take)]))
    assert r[1].offset_us is None  # no audio, nothing to match
    assert r[10].offset_us is None  # unreadable speech file is not laid on the timeline
    assert r[11].offset_us == 0
    assert abs(r[2].offset_us - 10_000_000) <= 10_000


def test_tracks_nothing_with_audio():
    r = _by_id(sync_tracks({1: None}, {2: None}, []))
    assert r[1].offset_us is None and r[2].offset_us is None
