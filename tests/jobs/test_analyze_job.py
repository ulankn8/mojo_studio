"""Worker job `analyze`: per-stage checkpoints, stop/resume, sync, progress and ETA."""

import itertools
from datetime import date
from pathlib import Path

from tests.jobs.worker_run import events, is_stage, kill_tree, run_raw, run_until, run_worker
from tests.media_fixtures import (
    APPLAUSE_WAV,
    make_clip,
    make_noise_clip,
    make_toast_clip,
    make_truncated,
    make_wav_clip,
    needs_ffmpeg,
    noise_burst_track,
    read_wav16,
    write_wav16,
)
from toystudio.core.models import (
    STAGE_ORDER,
    MediaKind,
    MediaRole,
    MediaStatus,
    MomentType,
    ProjectFormat,
    StageName,
    StageState,
    ToiKind,
)
from toystudio.engine.media.probe import probe_media
from toystudio.jobs import analyze_job
from toystudio.jobs.protocol import Done, Failed, Progress
from toystudio.jobs.worker_main import JobContext
from toystudio.storage.app_store import AppStore
from toystudio.storage.project_store import ProjectStore

pytestmark = needs_ffmpeg

FINISHED = (StageState.DONE, StageState.SKIPPED)


def _project(tmp_path: Path, *clips: Path) -> Path:
    root = tmp_path / "Той Студия" / "Свадьба Айбек и Нурайым"
    s = ProjectStore.create(root, "Свадьба", ToiKind.WEDDING, date(2026, 9, 1))
    try:
        for c in clips:
            s.add_media(c, MediaKind.VIDEO, c.stat().st_size if c.exists() else 1, False)
    finally:
        s.close()
    return root


def _clip(tmp_path: Path, name: str, duration_s: float = 4) -> Path:
    return make_clip(tmp_path / "src" / name, duration_s=duration_s, size="320x180")


def test_analyze_two_files_and_sync(tmp_path: Path) -> None:
    # second clip = the same noise-burst track from 2 s: overlap 38 s >= 30 s (sync default)
    a = make_noise_clip(tmp_path / "src" / "камера 1.mp4", duration_s=40, track_s=40, seed=7)
    b = make_noise_clip(
        tmp_path / "src" / "камера 2.mp4", duration_s=38, start_s=2, track_s=40, seed=7
    )
    project = _project(tmp_path, a, b)
    app_db = tmp_path / "app.db"
    msgs: list = []
    s = run_worker("analyze", project, app_db=app_db, msgs=msgs)
    try:
        stages = s.stages()
        assert {(st.media_id, st.stage) for st in stages} == {
            *((mid, st) for mid in (1, 2) for st in STAGE_ORDER),
            (0, StageName.SYNC),
        }
        assert all(st.state in FINISHED for st in stages)
        assert s.stage(1, StageName.SPEECH).state is StageState.DONE
        assert s.stage(1, StageName.MOMENTS).state is StageState.DONE
        assert s.stage(1, StageName.PROXY).state is StageState.DONE
        rows = {m.id: m for m in s.list_media()}
        assert rows[1].sync_offset_us == 0
        assert abs(rows[2].sync_offset_us - 2_000_000) <= 20_000
        assert rows[2].sync_confidence >= 6.0
        assert rows[1].info is not None  # PROBE filled info without a prior import
        assert s.step() == "analysis_done"
    finally:
        s.close()
    assert (project / "cache" / "proxy" / "1.mp4").exists()
    assert (project / "cache" / "proxy" / "2.mp4").exists()
    card = project / "cache" / "thumbs" / "card.jpg"
    assert card.exists() and card.read_bytes()[:2] == b"\xff\xd8"
    assert not list((project / "cache").rglob("*.part"))

    proxy = [m for m in msgs if isinstance(m, Progress) and m.stage == "proxy"]
    assert {m.media_id for m in proxy} == {"1", "2"}
    assert proxy[-1].done == proxy[-1].total and abs(proxy[-1].total - 38.0) < 0.5
    found = {(e["media_id"], e["stage"]): e["found"] for e in events(msgs, "stage")
             if e["state"] == "done"}  # fmt: skip
    assert isinstance(found[(1, "scenes")], int) and isinstance(found[(1, "quality")], int)
    assert found[(1, "proxy")] is None
    eta = events(msgs, "eta")
    assert eta and all(isinstance(e["remaining_s"], float) and e["remaining_s"] >= 0 for e in eta)
    app = AppStore(app_db)
    try:
        assert app.speed("proxy", "light") is not None
    finally:
        app.close()


def test_analyze_continues_after_corrupt(tmp_path: Path) -> None:
    good = _clip(tmp_path, "good.mp4")
    corrupt = make_truncated(good, tmp_path / "src" / "битый.mp4", keep=0.5)
    # passes the import (info set from the good file) but ffmpeg cannot decode it
    broken = make_truncated(good, tmp_path / "src" / "сломан.mp4", keep=0.4)
    project = _project(tmp_path, corrupt, broken, good)
    s = ProjectStore.open(project)
    s.set_media_info(2, probe_media(good))
    s.close()

    msgs: list = []
    s = run_worker("analyze", project, app_db=tmp_path / "app.db", msgs=msgs)
    try:
        rows = {m.id: m for m in s.list_media()}
        assert rows[1].status is MediaStatus.CORRUPT
        probe1 = s.stage(1, StageName.PROBE)
        assert probe1.state is StageState.FAILED and probe1.error_code == "corrupt"
        assert s.stage(1, StageName.PROXY).state is StageState.PENDING
        proxy2 = s.stage(2, StageName.PROXY)
        assert proxy2.state is StageState.FAILED and proxy2.error_code == "ffmpeg"
        assert all(s.stage(3, st).state in FINISHED for st in STAGE_ORDER)
        assert s.stage(0, StageName.SYNC).state is StageState.DONE
        assert s.step() == "analysis_done"  # ruling R9: a failed file is finished
    finally:
        s.close()
    assert {"media_id": 1, "status": "corrupt"} in events(msgs, "media")
    assert any(
        e["media_id"] == 2 and e["stage"] == "proxy" and e["state"] == "failed"
        for e in events(msgs, "stage")
    )
    assert not (project / "cache" / "proxy" / "2.mp4").exists()
    assert (project / "cache" / "proxy" / "3.mp4").exists()


def test_analyze_all_corrupt_ends_analysis_done(tmp_path: Path) -> None:
    good = _clip(tmp_path, "good.mp4", duration_s=2)
    corrupt = make_truncated(good, tmp_path / "src" / "битый.mp4", keep=0.5)
    project = _project(tmp_path, corrupt)
    s = run_worker("analyze", project, app_db=tmp_path / "app.db")
    try:
        assert s.list_media()[0].status is MediaStatus.CORRUPT
        assert s.step() == "analysis_done"
    finally:
        s.close()


def test_analyze_marks_missing_file(tmp_path: Path) -> None:
    gone = _clip(tmp_path, "пропал.mp4", duration_s=2)
    good = _clip(tmp_path, "good.mp4", duration_s=3)
    project = _project(tmp_path, gone, good)
    gone.unlink()
    msgs: list = []
    s = run_worker("analyze", project, app_db=tmp_path / "app.db", msgs=msgs)
    try:
        rows = {m.id: m for m in s.list_media()}
        assert rows[1].status is MediaStatus.MISSING
        assert rows[2].status is MediaStatus.OK
        assert all(s.stage(2, st).state in FINISHED for st in STAGE_ORDER)
        assert s.stage(1, StageName.PROXY).state is StageState.PENDING
        assert s.step() == "analysis"  # a missing file keeps the analysis open
    finally:
        s.close()
    assert {"media_id": 1, "status": "missing"} in events(msgs, "media")


def test_analyze_resumes_after_kill(tmp_path: Path) -> None:
    first = _clip(tmp_path, "первый.mp4", duration_s=4)
    second = _clip(tmp_path, "второй.mp4", duration_s=60)  # still in PROXY when killed
    project = _project(tmp_path, first, second)
    app_db = tmp_path / "app.db"

    rc, msgs = run_until(
        "analyze",
        project,
        app_db=app_db,
        when=is_stage("proxy", "done", 1),
        then=kill_tree,
    )
    assert rc != 0 and not any(isinstance(m, Done) for m in msgs)

    s = ProjectStore.open(project)  # running -> interrupted
    try:
        assert s.stage(1, StageName.PROXY).state is StageState.DONE
        assert s.stage(2, StageName.PROXY).state in (StageState.PENDING, StageState.INTERRUPTED)
        assert s.stage(0, StageName.SYNC).state is StageState.PENDING
        assert s.step() == "analysis"
    finally:
        s.close()
    proxy1 = project / "cache" / "proxy" / "1.mp4"
    mtime = proxy1.stat().st_mtime_ns

    msgs2: list = []
    s = run_worker("analyze", project, app_db=app_db, msgs=msgs2)
    try:
        assert all(st.state in FINISHED for st in s.stages())
        assert s.step() == "analysis_done"
    finally:
        s.close()
    assert proxy1.stat().st_mtime_ns == mtime
    assert not [
        m for m in msgs2 if isinstance(m, Progress) and (m.stage, m.media_id) == ("proxy", "1")
    ]
    assert (project / "cache" / "proxy" / "2.mp4").exists()


def test_analyze_stop_file_interrupts_stage(tmp_path: Path) -> None:
    clip = _clip(tmp_path, "длинный.mp4", duration_s=60)
    project = _project(tmp_path, clip)
    stop = tmp_path / "stop.flag"

    rc, msgs = run_until(
        "analyze",
        project,
        app_db=tmp_path / "app.db",
        stop_file=stop,
        when=is_stage("proxy", "running", 1),
        then=lambda proc: stop.touch(),
    )
    assert rc == 1
    assert msgs[-1] == Failed("stopped", "")
    assert not any(isinstance(m, Done) for m in msgs)

    s = ProjectStore.open(project)
    try:
        assert s.stage(1, StageName.PROBE).state is StageState.DONE
        assert s.stage(1, StageName.PROXY).state is StageState.INTERRUPTED
    finally:
        s.close()
    assert not (project / "cache" / "proxy" / "1.mp4").exists()
    assert not list((project / "cache").rglob("*.part"))

    stop.unlink()  # "Продолжить": the same job runs again and finishes
    rc, msgs, _err = run_raw("analyze", project, app_db=tmp_path / "app.db", stop_file=stop)
    assert rc == 0 and isinstance(msgs[-1], Done)


class _Ctx(JobContext):
    """In-process context that collects messages instead of writing stdout."""

    def __init__(self, project: Path, app_db: Path) -> None:
        super().__init__(str(project), "in-process", None, app_db)
        self.msgs: list = []

    def report(self, msg) -> None:  # type: ignore[no-untyped-def]
        self.msgs.append(msg)


def test_analyze_unexpected_error_fails_only_that_file(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path, _clip(tmp_path, "первый.mp4", 2), _clip(tmp_path, "второй.mp4", 3))
    real = analyze_job.detect_cuts
    calls = []

    def flaky(m, fps=4.0):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("bad metrics")
        return real(m, fps=fps)

    monkeypatch.setattr(analyze_job, "detect_cuts", flaky)
    ctx = _Ctx(project, tmp_path / "app.db")
    rc = worker_main_run(ctx)
    assert rc == 0 and isinstance(ctx.msgs[-1], Done)
    s = ProjectStore.open(project)
    try:
        scenes1 = s.stage(1, StageName.SCENES)
        assert scenes1.state is StageState.FAILED and scenes1.error_code == "error"
        assert s.stage(1, StageName.QUALITY).state is StageState.PENDING
        assert all(s.stage(2, st).state in FINISHED for st in STAGE_ORDER)
        assert s.stage(0, StageName.SYNC).state is StageState.DONE
        assert s.step() == "analysis_done"
    finally:
        s.close()
    assert any(
        e["media_id"] == 1 and e["stage"] == "scenes" and e["state"] == "failed"
        for e in events(ctx.msgs, "stage")
    )


def worker_main_run(ctx: JobContext) -> int:
    """worker_main.main's job wrapper (Stopped / exception / Done) around analyze_job.run."""
    from toystudio.jobs.protocol import Failed as _Failed

    try:
        analyze_job.run(ctx)
    except Exception as e:  # noqa: BLE001
        ctx.report(_Failed("exception", repr(e)))
        return 1
    ctx.report(Done())
    return 0


def test_missing_file_that_is_back_is_analyzed_on_rerun(tmp_path: Path) -> None:
    first = _clip(tmp_path, "первый.mp4", duration_s=3)
    second = _clip(tmp_path, "второй.mp4", duration_s=3)
    project = _project(tmp_path, first, second)
    app_db = tmp_path / "app.db"
    run_worker("analyze", project, app_db=app_db).close()
    hidden = first.with_name("спрятан.mp4")
    proxy1 = project / "cache" / "proxy" / "1.mp4"
    mtime = proxy1.stat().st_mtime_ns
    first.rename(hidden)
    s = ProjectStore.open(project)
    try:
        s.set_media_status(1, MediaStatus.MISSING)
    finally:
        s.close()
    hidden.rename(first)
    msgs: list = []
    s = run_worker("analyze", project, app_db=app_db, msgs=msgs)
    try:
        assert s.list_media()[0].status is MediaStatus.OK
        assert all(s.stage(1, st).state in FINISHED for st in STAGE_ORDER)
        assert s.step() == "analysis_done"
    finally:
        s.close()
    assert proxy1.stat().st_mtime_ns == mtime  # finished stages are reused
    assert {"media_id": 1, "status": "ok"} in events(msgs, "media")


def test_unexpected_sync_error_marks_sync_failed_and_finishes(tmp_path: Path, monkeypatch) -> None:
    clip = _clip(tmp_path, "клип.mp4", duration_s=3)
    project = _project(tmp_path, clip)

    def boom(*_args):
        raise RuntimeError("sync broke")

    monkeypatch.setattr(analyze_job, "sync_tracks", boom)
    ctx = _Ctx(project, tmp_path / "app.db")
    rc = worker_main_run(ctx)
    assert rc == 0 and isinstance(ctx.msgs[-1], Done)
    s = ProjectStore.open(project)
    try:
        sync = s.stage(0, StageName.SYNC)
        assert sync.state is StageState.FAILED and sync.error_code == "error"
        assert s.step() == "analysis_done"
    finally:
        s.close()


# --- stage 2 (speech regions, audio events) and stage 5 (moments) ---

SR = 16_000
MOMENT_KEYS = {
    "media_id",
    "moment_id",
    "type",
    "start_us",
    "end_us",
    "score",
    "preview",
    "quality_label",
    "reasons",
}


def _applause_clip(tmp_path: Path) -> Path:
    return make_wav_clip(tmp_path / "src" / "кол чабуу.mp4", APPLAUSE_WAV)


def _found(msgs: list, media_id: int, stage: str) -> object:
    (e,) = [e for e in events(msgs, "stage") if (e["media_id"], e["stage"], e["state"])
            == (media_id, stage, "done")]  # fmt: skip
    return e["found"]


def test_analyze_finds_toast_and_applause(tmp_path: Path) -> None:
    project = _project(tmp_path, make_toast_clip(tmp_path))
    msgs: list = []
    s = run_worker("analyze", project, app_db=tmp_path / "app.db", msgs=msgs)
    try:
        assert s.stage(1, StageName.SPEECH).state is StageState.DONE
        regions = s.speech_regions(1)
        assert regions and _found(msgs, 1, "speech") == len(regions)
        moments = s.moments(1)
        by_type = {m.type: m for m in moments}
        assert MomentType.TOAST in by_type and MomentType.APPLAUSE in by_type, moments
        toast, applause = by_type[MomentType.TOAST], by_type[MomentType.APPLAUSE]
        assert toast.start_us < 3_000_000 and toast.end_us > 25_000_000
        assert applause.start_us >= 28_000_000
        assert _found(msgs, 1, "moments") == len(moments)
    finally:
        s.close()
    speech = [m for m in msgs if isinstance(m, Progress) and m.stage == "speech" and m.total > 1]
    assert speech and abs(speech[-1].total - 40.0) < 0.5
    assert speech[-1].done == speech[-1].total
    assert all(a.done <= b.done for a, b in itertools.pairwise(speech))


def test_analyze_no_audio_skips_speech_and_finds_nothing(tmp_path: Path) -> None:
    clip = make_clip(tmp_path / "src" / "тихо.mp4", duration_s=3, size="320x180", audio=False)
    project = _project(tmp_path, clip)
    msgs: list = []
    s = run_worker("analyze", project, app_db=tmp_path / "app.db", msgs=msgs)
    try:
        assert s.stage(1, StageName.SPEECH).state is StageState.SKIPPED
        assert s.stage(1, StageName.MOMENTS).state is StageState.DONE
        assert s.speech_regions(1) == [] and s.audio_events(1) == [] and s.moments() == []
        assert s.step() == "analysis_done"
    finally:
        s.close()
    skipped = [
        e for e in events(msgs, "stage") if e["stage"] == "speech" and e["state"] == "skipped"
    ]
    assert skipped == [{"media_id": 1, "stage": "speech", "state": "skipped", "found": None}]
    assert _found(msgs, 1, "moments") == 0
    assert events(msgs, "moment") == []


class _StopInSpeech(_Ctx):
    """Asks to stop as soon as the SPEECH stage of any file has started."""

    def should_stop(self) -> bool:
        return any(is_stage("speech", "running")(m) for m in self.msgs)


def test_stop_during_speech_resumes_without_duplicates(tmp_path: Path) -> None:
    project = _project(tmp_path, _applause_clip(tmp_path))
    app_db = tmp_path / "app.db"
    ctx = _StopInSpeech(project, app_db)
    assert worker_main_run(ctx) == 1
    s = ProjectStore.open(project)
    try:
        assert s.stage(1, StageName.PROXY).state is StageState.DONE
        assert s.stage(1, StageName.SPEECH).state is StageState.INTERRUPTED
        assert s.stage(1, StageName.MOMENTS).state is StageState.PENDING
        assert s.audio_events(1) == [] and s.moments() == []
    finally:
        s.close()

    def full_run() -> tuple[list, list, list]:
        c = _Ctx(project, app_db)
        assert worker_main_run(c) == 0
        st = ProjectStore.open(project)
        try:
            assert all(r.state in FINISHED for r in st.stages())
            return st.speech_regions(1), st.audio_events(1), st.moments(1)
        finally:
            st.close()

    regions, evs, moments = full_run()
    assert evs and moments
    thumbs = project / "cache" / "thumbs"
    assert sorted(p.name for p in thumbs.glob("m*.jpg")) == sorted(f"m{m.id}.jpg" for m in moments)

    # a stop after the stages wrote their rows: the rerun recomputes them, nothing doubles
    s = ProjectStore.open(project)
    try:
        s.set_stage(1, StageName.SPEECH, StageState.INTERRUPTED)
        s.set_stage(1, StageName.MOMENTS, StageState.INTERRUPTED)
    finally:
        s.close()
    regions2, evs2, moments2 = full_run()
    assert regions2 == regions and evs2 == evs
    assert [(m.type, m.start_us, m.end_us) for m in moments2] == [
        (m.type, m.start_us, m.end_us) for m in moments
    ]
    assert min(m.id for m in moments2) > max(m.id for m in moments)  # ids are never reused
    assert sorted(p.name for p in thumbs.glob("m*.jpg")) == sorted(
        f"m{m.id}.jpg" for m in moments2
    )  # old previews are removed


def test_moment_events_sent_with_preview(tmp_path: Path) -> None:
    project = _project(tmp_path, _applause_clip(tmp_path))
    msgs: list = []
    s = run_worker("analyze", project, app_db=tmp_path / "app.db", msgs=msgs)
    try:
        stored = {m.id: m for m in s.moments(1)}
    finally:
        s.close()
    sent = events(msgs, "moment")
    assert sent and len(sent) == len(stored)
    assert any(e["type"] == "applause" for e in sent)
    for e in sent:
        assert set(e) == MOMENT_KEYS and e["media_id"] == 1
        m = stored[e["moment_id"]]
        assert (e["type"], e["start_us"], e["end_us"]) == (m.type.value, m.start_us, m.end_us)
        assert e["score"] == m.score
        assert e["preview"] == m.preview == f"cache/thumbs/m{m.id}.jpg"
        jpg = project / e["preview"]
        assert jpg.is_file() and jpg.read_bytes()[:2] == b"\xff\xd8"
    payloads = [m.payload for m in msgs if hasattr(m, "payload")]
    done = payloads.index({"media_id": 1, "stage": "moments", "state": "done", "found": len(sent)})
    assert all(payloads.index(e) < done for e in sent)
    assert not list((project / "cache").rglob("*.part"))


def test_moments_grouped_after_sync(tmp_path: Path) -> None:
    # one 40 s track (noise bursts for sync, the CC0 applause at 15-25 s); camera 2 starts at 2 s
    track = noise_burst_track(40, seed=7)
    track[15 * SR : 25 * SR] = read_wav16(APPLAUSE_WAV)
    src = tmp_path / "src"
    a = make_wav_clip(src / "камера 1.mp4", write_wav16(src / "a.wav", track))
    b = make_wav_clip(src / "камера 2.mp4", write_wav16(src / "b.wav", track[2 * SR :]))
    project = _project(tmp_path, a, b)
    s = run_worker("analyze", project, app_db=tmp_path / "app.db")
    try:
        assert s.stage(0, StageName.SYNC).state is StageState.DONE
        rows = {m.id: m for m in s.list_media()}
        assert abs(rows[2].sync_offset_us - 2_000_000) <= 20_000
        claps = [m for m in s.moments() if m.type is MomentType.APPLAUSE]
        assert {m.media_id for m in claps} == {1, 2}, s.moments()
        assert len({m.group_id for m in claps}) == 1
        assert claps[0].group_id == min(m.id for m in claps)
        assert all(m.group_id is not None for m in s.moments())
    finally:
        s.close()


def test_failed_preview_or_grouping_does_not_fail_analysis(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path, _applause_clip(tmp_path))

    def no_frame(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("ffmpeg broke")

    def no_groups(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("grouping broke")

    monkeypatch.setattr(analyze_job, "frame_at", no_frame)
    monkeypatch.setattr(analyze_job, "group_ids", no_groups)
    ctx = _Ctx(project, tmp_path / "app.db")
    assert worker_main_run(ctx) == 0 and isinstance(ctx.msgs[-1], Done)
    s = ProjectStore.open(project)
    try:
        assert s.stage(1, StageName.MOMENTS).state is StageState.DONE
        assert s.stage(0, StageName.SYNC).state is StageState.DONE
        moments = s.moments(1)
        assert moments and all(m.preview is None and m.group_id is None for m in moments)
    finally:
        s.close()
    assert all(e["preview"] is None for e in events(ctx.msgs, "moment"))


# ---- whole-branch review fixes (phase 2b)
_NO_ORT = (
    "import sys; sys.modules['onnxruntime'] = None; "  # `import onnxruntime` now raises
    "from toystudio.jobs import worker_main; sys.exit(worker_main.main(sys.argv[1:]))"
)


def test_import_and_noop_jobs_run_without_onnxruntime(tmp_path: Path) -> None:
    import subprocess
    import sys

    from tests.jobs.worker_run import parse

    project = _project(tmp_path, _clip(tmp_path, "первый.mp4", 2))
    for kind in ("noop", "import"):
        args = ["--job", kind, "--project", str(project), "--job-id", f"t-{kind}"]
        r = subprocess.run(
            [sys.executable, "-c", _NO_ORT, *args, "--app-db", str(tmp_path / "app.db")],
            capture_output=True,
            check=False,
            timeout=120,
        )
        msgs = parse(r.stdout)
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert msgs and isinstance(msgs[-1], Done), msgs[-3:]


def test_broken_model_path_fails_speech_and_continues(tmp_path: Path, monkeypatch) -> None:
    from toystudio.engine import models

    project = _project(tmp_path, _clip(tmp_path, "первый.mp4", 2), _clip(tmp_path, "второй.mp4", 3))
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "нет моделей")
    ctx = _Ctx(project, tmp_path / "app.db")
    assert worker_main_run(ctx) == 0 and isinstance(ctx.msgs[-1], Done)
    s = ProjectStore.open(project)
    try:
        for mid in (1, 2):
            speech = s.stage(mid, StageName.SPEECH)
            assert speech.state is StageState.FAILED and speech.error_code == "error"
            assert s.stage(mid, StageName.PROXY).state is StageState.DONE
        assert s.stage(0, StageName.SYNC).state is StageState.DONE
        assert s.step() == "analysis_done"
    finally:
        s.close()


def test_moments_rerun_when_an_earlier_stage_ran(tmp_path: Path) -> None:
    project = _project(tmp_path, _clip(tmp_path, "первый.mp4", 2))
    app_db = tmp_path / "app.db"
    assert worker_main_run(_Ctx(project, app_db)) == 0
    s = ProjectStore.open(project)
    try:
        assert s.stage(1, StageName.MOMENTS).state is StageState.DONE
        s.set_stage(1, StageName.SCENES, StageState.INTERRUPTED)  # MOMENTS row stays current
    finally:
        s.close()
    ctx = _Ctx(project, app_db)
    assert worker_main_run(ctx) == 0
    ran = [(e["stage"], e["state"]) for e in events(ctx.msgs, "stage") if e["media_id"] == 1]
    assert ("scenes", "running") in ran and ("moments", "running") in ran
    assert ("quality", "running") not in ran  # a stage that does not depend on it is reused
    # nothing changed: a third run does no per-file work at all
    ctx = _Ctx(project, app_db)
    assert worker_main_run(ctx) == 0
    assert not [e for e in events(ctx.msgs, "stage") if e["media_id"] == 1]


# --- tracks: speech from the sound desk, mixed shooting, music library ---


def _tracks_project(tmp_path: Path, files: list[tuple[Path, MediaRole]]) -> Path:
    root = tmp_path / "Той Студия" / "Свадьба полностью"
    s = ProjectStore.create(
        root, "Свадьба", ToiKind.WEDDING, date(2026, 6, 14), ProjectFormat.WEDDING_FULL
    )
    try:
        for path, role in files:
            s.add_media(path, role, path.stat().st_size, False)
    finally:
        s.close()
    return root


def test_speech_recording_split_in_files_is_the_restaurant_reference(tmp_path: Path) -> None:
    # recorder take of 80 s split into two wav files at 40 s; V1 clip = 35 s from 42 s
    take = noise_burst_track(80, seed=7)
    rate = 16_000
    rec1 = write_wav16(tmp_path / "src" / "071424-011212.wav", take[: 40 * rate])
    rec2 = write_wav16(tmp_path / "src" / "071424-011252.wav", take[40 * rate :])
    main = make_noise_clip(
        tmp_path / "src" / "штатив.mp4", duration_s=35, start_s=42, track_s=80, seed=7
    )
    pre = _clip(tmp_path, "дом жениха.mp4", duration_s=3)
    song = write_wav16(tmp_path / "src" / "песня.wav", noise_burst_track(5, seed=3))
    project = _tracks_project(
        tmp_path,
        [
            (rec2, MediaRole.SPEECH),  # added out of order: names give the recording order
            (rec1, MediaRole.SPEECH),
            (main, MediaRole.MAIN),
            (pre, MediaRole.PRE),
            (song, MediaRole.MUSIC),
        ],
    )
    s = run_worker("analyze", project, app_db=tmp_path / "app.db")
    try:
        rows = {m.path.name: m for m in s.list_media()}
        r1, r2 = rows["071424-011212.wav"], rows["071424-011252.wav"]
        for rec in (r1, r2):
            assert s.stage(rec.id, StageName.PROXY).state is StageState.DONE
            assert s.stage(rec.id, StageName.SPEECH).state is StageState.DONE
            for st in (StageName.SCENES, StageName.QUALITY, StageName.MOMENTS):
                assert s.stage(rec.id, st).state is StageState.SKIPPED
        assert r1.sync_offset_us == 0 and r2.sync_offset_us == 40_000_000
        v1 = rows["штатив.mp4"]
        assert abs(v1.sync_offset_us - 42_000_000) <= 20_000 and v1.sync_confidence >= 6.0
        assert rows["дом жениха.mp4"].sync_offset_us is None  # no common clock before
        assert s.stage(rows["дом жениха.mp4"].id, StageName.SCENES).state is StageState.DONE
        assert s.stage(rows["песня.wav"].id, StageName.PROBE) is None  # music is not analysed
        assert s.step() == "analysis_done"
    finally:
        s.close()


def test_speech_file_that_is_not_audio_is_marked_corrupt(tmp_path: Path) -> None:
    bad = tmp_path / "src" / "пульт.wav"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a wav at all" * 100)
    main = _clip(tmp_path, "штатив.mp4", duration_s=3)
    project = _tracks_project(tmp_path, [(bad, MediaRole.SPEECH), (main, MediaRole.MAIN)])
    s = run_worker("analyze", project, app_db=tmp_path / "app.db")
    try:
        rows = {m.path.name: m for m in s.list_media()}
        assert rows["пульт.wav"].status is MediaStatus.CORRUPT
        assert s.stage(rows["штатив.mp4"].id, StageName.MOMENTS).state is StageState.DONE
        assert s.step() == "analysis_done"
    finally:
        s.close()
