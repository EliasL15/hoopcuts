"""Version 2 recovery, grouping, persistence and real decoder regressions."""
from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import cv2
import numpy as np

from test_analysis_recovery import snapshots
from test_billing import env
from test_game_context import pipeline as legacy_pipeline
from basketball_highlights import cli, pipeline, decisions
from basketball_highlights.budget import Budget
from basketball_highlights.journal import Journal
from basketball_highlights.models import ContextResult, Detection
from basketball_highlights.retry import RequestFailed, RequestGate
from basketball_highlights.video import FrameReader, SampledFrame, ffprobe, sample_frame
from hosted import worker, checkpoints


@pytest.fixture
def fast_pipeline(legacy_pipeline, monkeypatch):
    fixture = legacy_pipeline
    fixture.args.pipeline_version = 2
    monkeypatch.setattr(pipeline, "ffprobe", cli.ffprobe)
    monkeypatch.setattr(pipeline, "DecisionsAdapter", lambda: fixture.adapter)
    class Reader:
        def __init__(self, *args, **kwargs):
            pass
        def sample(self, timestamp):
            return cli.sample_frame(fixture.args.video, timestamp)
        def close(self):
            pass
    monkeypatch.setattr(pipeline, "FrameReader", Reader)
    original = cli.create_clip
    monkeypatch.setattr(cli, "create_clip", lambda *args, **kwargs: original(*args))
    return fixture


def test_v2_matches_legacy_results_and_resumes_without_requests(fast_pipeline, tmp_path):
    p = fast_pipeline
    p.args.pipeline_version = 1
    cli.analyze(p.args)
    expected = json.loads((p.output / "events.json").read_text())
    p.args.pipeline_version, p.args.output_dir = 2, tmp_path / "v2"
    cli.analyze(p.args)
    assert json.loads((p.args.output_dir / "events.json").read_text()) == expected
    calls, checks = p.adapter.outcomes, len(p.adapter.checks)
    p.args.resume = True
    cli.analyze(p.args)
    assert (p.adapter.outcomes, len(p.adapter.checks)) == (calls, checks)
    report = json.loads((p.args.output_dir / "performance.json").read_text())
    assert report["pipeline_version"] == 2 and report["group_windows"] == 1
    assert report["completed_windows"] > 0 and report["stage_seconds"]["persistence"] > 0


def test_v2_failure_resumes_only_unfinished_windows(fast_pipeline):
    p = fast_pipeline
    original = p.adapter.classify
    seen = []
    def interrupted(frames):
        seen.append(frames[0].timestamp)
        if len(seen) == 3:
            raise RequestFailed("rate_limit", True, 429)
        return original(frames)
    p.adapter.classify = interrupted
    with pytest.raises(RequestFailed):
        cli.analyze(p.args)
    assert len(json.loads((p.output / "run_state.json").read_text())["detections"]) == 2
    resumed = []
    def classify(frames):
        resumed.append(frames[0].timestamp)
        return original(frames)
    p.adapter.classify = classify
    p.args.resume = True
    cli.analyze(p.args)
    assert not set(seen[:2]).intersection(resumed)
    assert json.loads((p.output / "processing_summary.json").read_text())["complete"]


def test_v2_checkpoint_failure_prevents_dispatch(fast_pipeline):
    calls = []
    def checkpoint(state, ledger):
        calls.append((state, ledger))
        if len(calls) == 2:
            assert ledger["pending"]
            raise checkpoints.CheckpointError("storage failure")
    with pytest.raises(checkpoints.CheckpointError):
        cli.analyze(fast_pipeline.args, checkpoint_callback=checkpoint)
    assert fast_pipeline.adapter.outcomes == 0


def test_v2_delta_restore_and_compaction(snapshots, fast_pipeline, tmp_path):
    ref, objects, persistence = snapshots
    p = fast_pipeline
    persistence.config = {**persistence.config, "pipeline_version": 2}
    identity = persistence.identity()
    cli.analyze(p.args, checkpoint_callback=lambda state, budget: persistence.publish(identity, state, budget), source_identity=identity)
    pointer = ref.get().to_dict()["analysis_checkpoint"]
    assert pointer["segments"]
    restored = tmp_path / "restored"
    persistence.restore(restored, identity)
    assert json.loads((restored / "run_state.json").read_text())["detections"] == json.loads((p.output / "run_state.json").read_text())["detections"]
    state, budget = json.loads((restored / "run_state.json").read_text()), json.loads((restored / "budget.json").read_text())
    assert budget == json.loads((p.output / "budget.json").read_text())
    state.update(journal_delta=False, completed=len(state["detections"]))
    persistence.publish(identity, state, budget)
    assert "segments" not in ref.get().to_dict()["analysis_checkpoint"]
    assert len(objects) == 1
    persistence.restore(tmp_path / "compacted", identity)


def test_delta_missing_segment_or_ledger_offset_fails_atomically(snapshots, tmp_path):
    ref, objects, persistence = snapshots
    journal = Journal(tmp_path / "state.sqlite3")
    signature = {"duration": 20, "window_seconds": .75, "stride_seconds": .25}
    journal.set_meta("signature", signature)
    budget = Budget(tmp_path / "budget.json", 5, journal=journal)
    state, ledger = journal.checkpoint(signature, [], 0, compact=True)
    identity = persistence.identity()
    persistence.publish(identity, state, ledger)
    journal.published()
    budget.reserve(3, attempts=4)
    state, ledger = journal.checkpoint(signature, [], 0)
    ledger["requests_offset"] = 9
    persistence.publish(identity, state, ledger)
    with pytest.raises(checkpoints.CheckpointError):
        persistence.restore(tmp_path / "bad", identity)
    assert not (tmp_path / "bad" / "budget.json").exists()
    pointer = ref.get().to_dict()["analysis_checkpoint"]
    objects.pop(pointer["segments"][0]["key"])
    with pytest.raises(checkpoints.CheckpointError):
        persistence.restore(tmp_path / "missing", identity)
    journal.close()


def test_journal_transactions_and_pending_recovery(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path)
    budget = Budget(tmp_path / "budget.json", 5, journal=journal)
    reserve = budget.reserve(3, attempts=4)
    with pytest.raises(RuntimeError):
        with journal.transaction():
            journal.put("detections", "w1", {"classification": "made_basket"})
            raise RuntimeError("interrupt transaction")
    assert journal.records("detections") == {}
    journal.close()
    journal = Journal(path)
    resumed = Budget(tmp_path / "budget.json", 5, journal=journal)
    assert resumed.spend == reserve.amount_usd
    assert not resumed.data["pending"]
    assert len(resumed.data["requests"]) == 1
    assert not (tmp_path / "budget.json").exists()
    journal.close()


def test_indexed_sampling_matches_original_schedule():
    times = [n / 4 for n in range(100)]
    for left, right in [(0, .75), (.25, 1), (24.5, 25), (.1, .2), (.1, 7)]:
        expected = [t for t in times if left <= t < right] or [left]
        if len(expected) > 3:
            expected = [expected[round(i * (len(expected)-1) / 2)] for i in range(3)]
        assert pipeline.window_samples(times, left, right) == expected


def grouped_adapter(monkeypatch, missing=False):
    adapter = decisions.DecisionsAdapter.__new__(decisions.DecisionsAdapter)
    adapter.retries = 0
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        answers = [{"type": "choice", "name": question["name"], "choice": "made_basket" if question["name"].startswith("outcome") else "two_pointer",
                    "confidence": .98, "probabilities": [{"value": "made_basket", "probability": .95}]} for question in kwargs["questions"]]
        if missing:
            answers.pop()
        return SimpleNamespace(id="group", model_dump=lambda: {"answers": answers, "usage": {"input_tokens": 100}})
    adapter.client = SimpleNamespace(decisions=SimpleNamespace(create=create))
    return adapter, requests


def test_group_mapping_deduplicates_frames_preserves_probabilities(monkeypatch):
    adapter, requests = grouped_adapter(monkeypatch)
    windows = {"w000000": [SampledFrame(t, b"jpeg") for t in (0, .25, .5)],
               "w000001": [SampledFrame(t, b"jpeg") for t in (.25, .5, .75)]}
    result, usage = adapter.classify_group(windows)
    assert len([item for item in requests[0]["input"][0]["content"] if item["type"] == "input_image"]) == 4
    assert result["w000000"].estimated_event_timestamp == .25
    assert result["w000001"].evidence_timestamps == [.25, .5, .75]
    from basketball_highlights.events import made_basket_probability
    assert made_basket_probability(result["w000001"]) == .95
    assert usage == {"input_tokens": 100} and len(requests) == 1


def test_group_missing_answer_fails_without_completing_windows(monkeypatch):
    adapter, _ = grouped_adapter(monkeypatch, missing=True)
    with pytest.raises(RequestFailed, match="invalid_request"):
        adapter.classify_group({"w000000": [SampledFrame(0, b"jpeg")]})


@pytest.mark.parametrize("group_size", [4, 8])
def test_v2_grouped_request_counts_and_budget_once(fast_pipeline, group_size):
    p = fast_pipeline
    p.args.group_windows = group_size
    calls = []
    def classify_group(windows):
        calls.append(list(windows))
        return {key: Detection(classification="missed_shot", estimated_event_timestamp=frames[len(frames)//2].timestamp,
                               evidence_timestamps=[frame.timestamp for frame in frames]) for key, frames in windows.items()}, {"input_tokens": 100}
    p.adapter.classify_group = classify_group
    cli.analyze(p.args)
    state = json.loads((p.output / "run_state.json").read_text())
    budget = json.loads((p.output / "budget.json").read_text())
    count = len(state["detections"])
    assert len(calls) == (count+group_size-1)//group_size
    assert budget["request_count"] == len(calls) and budget["input_tokens"] == len(calls)*100


def test_allowance_owner_loss_is_not_retried(monkeypatch):
    adapter, requests = grouped_adapter(monkeypatch)
    adapter.retries = 3
    def check(*args):
        raise worker.WorkerLeaseLost("stale")
    adapter.allowance = check
    with pytest.raises(worker.WorkerLeaseLost):
        adapter.classify([SampledFrame(0, b"jpeg")])
    assert requests == [] and adapter.last_attempts == 0


def test_shared_allowance_is_coordinated_across_workers(env, monkeypatch):
    from hosted import allowance
    store, _ = env
    monkeypatch.setattr(allowance, "client", lambda: store)
    first = allowance.HostedAllowance(20, 100000, lambda: None, clock=lambda: 100)
    second = allowance.HostedAllowance(20, 100000, lambda: None, clock=lambda: 100)
    assert first._lease(100, 100) == 0
    assert second._lease(100, 100) > 0
    assert second._lease(100, 160) == 0


def test_new_settings_are_pinned_and_old_settings_remain_legacy(env, monkeypatch):
    _, ref = env
    old = {key: value for key, value in worker.analysis_config().items() if key not in ("pipeline_version", "group_windows", "direct_source", "timestamp_decoder", "rate_rpm", "rate_tpm")}
    ref.update({"status": "queued", "analysis_settings": old})
    monkeypatch.setattr(worker, "settings", replace(worker.settings, analysis_pipeline_version=2, analysis_concurrency=16))
    claimed = worker.claim_run("alice", "a7c0c3aa-6af9-4d7c-8c54-2523fd65025b", "new")
    assert claimed["analysis_settings"] == old
    assert worker.analysis_config()["pipeline_version"] == 2 and worker.analysis_config()["group_windows"] == 1


@pytest.mark.parametrize("direct_source", [False, True])
def test_new_runs_pin_enabled_grouping_and_recovery_keeps_it(env, monkeypatch, direct_source):
    from datetime import timedelta
    from test_media import NOW, RUN
    _, ref = env
    ref.update({"status": "queued"})
    monkeypatch.setattr(worker, "settings", replace(worker.settings, analysis_pipeline_version=2,
        analysis_group_windows=8, analysis_direct_source=direct_source, analysis_timestamp_decoder=True))
    first = worker.claim_run("alice", RUN, "first")
    assert first["analysis_settings"]["group_windows"] == 8
    assert first["analysis_settings"]["direct_source"] is direct_source
    assert first["analysis_settings"]["timestamp_decoder"] is True
    ref.update({"lease_expires_at": NOW - timedelta(minutes=1)})
    monkeypatch.setattr(worker, "settings", replace(worker.settings, analysis_group_windows=1,
        analysis_direct_source=not direct_source, analysis_timestamp_decoder=False))
    resumed = worker.claim_run("alice", RUN, "resumed")
    assert resumed["analysis_settings"]["group_windows"] == 8
    assert resumed["analysis_settings"]["direct_source"] is direct_source
    assert resumed["analysis_settings"]["timestamp_decoder"] is True


def test_persistent_reader_matches_seek_reader_on_cfr(tmp_path):
    path = tmp_path / "test.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    reader = FrameReader(path, sequential=True, info=ffprobe(path))
    try:
        for timestamp in (0, .25, .5, .75, 1, 1.25, 1.5, 2.5):
            assert reader.sample(timestamp).jpeg == sample_frame(path, timestamp).jpeg
    finally:
        reader.close()


def timestamp_reference(path, info):
    """Independently decode every frame with FFmpeg, indexed by ffprobe PTS."""
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
        "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"])
    metadata = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "frame=best_effort_timestamp_time:format=start_time", "-of", "json", str(path)]))
    origin = float(metadata["format"].get("start_time", 0))
    times = [float(frame["best_effort_timestamp_time"]) - origin for frame in metadata["frames"]]
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, info["display_height"], info["display_width"], 3)
    assert len(times) == len(frames)
    def expected(timestamp):
        index = min(range(len(times)), key=lambda i: (round(abs(times[i] - timestamp), 6), -times[i]))
        ok, jpeg = cv2.imencode(".jpg", frames[index], [cv2.IMWRITE_JPEG_QUALITY, 85])
        assert ok
        return times[index], jpeg.tobytes()
    return expected


@pytest.mark.parametrize("variant", ["vfr", "rotated", "hd_silent", "quicktime_edit"])
@pytest.mark.parametrize("thread_type", ["SLICE", "AUTO"])
def test_decoder_and_direct_preview_edge_cases(tmp_path, variant, thread_type):
    from basketball_highlights.clips import create_clip
    path = tmp_path / "source.mp4"
    size = "1920x1080" if variant == "hd_silent" else "320x180"
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration=2"]
    if variant == "vfr":
        args += ["-vf", "setpts='if(lt(N,30),N/(30*TB),(N-30)/(15*TB)+1/TB)'", "-fps_mode", "vfr"]
    subprocess.run([*args, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    if variant == "quicktime_edit":
        edited = tmp_path / "edited.mov"
        # Stream-copy trimming uses a MOV edit list rather than re-encoding the
        # first GOP, as QuickTime does. Evidence must use the edited timeline.
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.2", "-i", str(path),
                        "-t", "1.8", "-c", "copy", str(edited)], check=True)
        path = edited
    if variant == "rotated":
        rotated = tmp_path / "rotated.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c", "copy", "-metadata:s:v:0", "rotate=90", str(rotated)], check=True)
        if ffprobe(rotated)["rotation"] == 0:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(path), "-c", "copy", str(rotated)], check=True)
        path = rotated
    original = path.read_bytes()
    info = ffprobe(path)
    reference = timestamp_reference(path, info) if variant == "vfr" else None
    reader = FrameReader(path, sequential=True, info=info, decoder_thread_type=thread_type)
    try:
        for timestamp in (0, .25, .5, .75, 1.25, .25, 1.75):
            sampled = reader.sample(timestamp)
            if reference:
                pts, jpeg = reference(timestamp)
                assert reader.timestamp_reader.selected_time == pytest.approx(pts, abs=1e-6)
                actual = cv2.imdecode(np.frombuffer(sampled.jpeg, np.uint8), cv2.IMREAD_COLOR)
                expected = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                # FFmpeg builds can differ slightly in YUV-to-BGR rounding.
                assert np.abs(actual.astype(float) - expected).mean() < 2
            else:
                assert sampled.jpeg == sample_frame(path, timestamp).jpeg
        if variant == "vfr":
            assert reader.timestamp_reader is not None
    finally:
        reader.close()
    preview = tmp_path / "preview.mp4"
    create_clip(path, preview, .25, 1.25, max_dimension=720, threads=1)
    preview_info = ffprobe(preview)
    assert preview_info["display_width"] <= 1280 and preview_info["display_height"] <= 720
    assert abs(preview_info["duration_seconds"] - 1) < .1
    assert not preview_info["has_audio"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("variant", ["b_frames", "edited_mov", "offset"])
@pytest.mark.parametrize("thread_type", ["SLICE", "AUTO"])
def test_timestamp_reader_follows_pts_across_seeks(tmp_path, monkeypatch, variant, thread_type):
    from basketball_highlights import video
    path = tmp_path / "vfr.mp4"
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=4",
        "-vf", "setpts='if(lt(N,60),N/(30*TB),(N-60)/(15*TB)+2/TB)'", "-fps_mode", "vfr",
        "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if variant == "offset":
        args += ["-output_ts_offset", "5"]
    subprocess.run([*args, str(path)], check=True)
    if variant == "edited_mov":
        edited = tmp_path / "edited.mov"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.2", "-i", str(path),
            "-t", "5", "-c", "copy", str(edited)], check=True)
        path = edited
    info = ffprobe(path)
    expected = timestamp_reference(path, info)
    seeks = []
    original = video._TimestampReader._seek
    def seek(reader, timestamp):
        seeks.append(timestamp)
        return original(reader, timestamp)
    monkeypatch.setattr(video._TimestampReader, "_seek", seek)
    reader = FrameReader(path, sequential=True, info=info, decoder_thread_type=thread_type)
    try:
        assert reader.timestamp_reader is not None
        # Repeated requests, frame-rate change, backward refinement and a distant
        # jump all use presentation times, never an average-FPS frame index.
        for timestamp in (0, 0, .25, .5, 1, 1.75, 2, 2.25, 2.5, 3, .25, 4.5, 4.75):
            sampled = reader.sample(timestamp)
            pts, jpeg = expected(timestamp)
            assert sampled.timestamp == timestamp
            assert reader.timestamp_reader.selected_time == pytest.approx(pts, abs=1e-6)
            actual = cv2.imdecode(np.frombuffer(sampled.jpeg, np.uint8), cv2.IMREAD_COLOR)
            reference = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            assert np.abs(actual.astype(float) - reference).mean() < 2
        assert seeks == [0, .25, 4.5]
        # An audio tail or invalid metadata must not make the sampler silently
        # duplicate the final image beyond the video stream's end.
        with pytest.raises(RuntimeError, match="Could not decode frame"):
            reader.sample(100)
    finally:
        reader.close()


def test_missing_pts_falls_back_to_existing_sampler(tmp_path, monkeypatch):
    from basketball_highlights import video
    path = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=320x180:rate=30:duration=2", "-c:v", "libx264", str(path)], check=True)
    reader = FrameReader(path, info={**ffprobe(path), "variable_frame_rate": True})
    def missing(timestamp):
        raise video._MissingFrameTimestamps("Missing PTS")
    monkeypatch.setattr(reader.timestamp_reader, "sample", missing)
    try:
        assert reader.sample(.5).jpeg == sample_frame(path, .5).jpeg
        assert reader.timestamp_reader is None
        assert reader.sample(.75).jpeg == sample_frame(path, .75).jpeg
    finally:
        reader.close()


def test_saved_decoder_mode_changes_require_new_analysis(fast_pipeline):
    p = fast_pipeline
    p.args.timestamp_decoder = True
    cli.analyze(p.args)
    p.args.resume = True
    p.args.timestamp_decoder = False
    with pytest.raises(ValueError, match="Resume settings"):
        cli.analyze(p.args)


def test_cancel_during_preview_preserves_results_and_performance(fast_pipeline):
    def progress(value):
        if value["phase"] == "creating_clips":
            raise worker.AnalysisCancelled()
    # Force progress after analysis despite the short fake video's elapsed time.
    # The first preview tick is normally throttled; simulate work lasting >5 sec
    # without slowing the suite or mutating the cancellation semantics.
    original = cli.create_clip
    clock = [0]
    from unittest.mock import patch
    def clip(*args, **kwargs):
        clock[0] += 6
        return original(*args, **kwargs)
    with patch.object(pipeline.time, "monotonic", lambda: clock[0]), patch.object(cli, "create_clip", clip):
        with pytest.raises(worker.AnalysisCancelled):
            cli.analyze(fast_pipeline.args, progress_callback=progress)
    assert (fast_pipeline.output / "events.json").exists()
    assert (fast_pipeline.output / "performance.json").exists()


@pytest.mark.parametrize("prior_cost", [0, .5])
def test_worker_v2_direct_source_uploads_and_exposes_metrics(snapshots, fast_pipeline, monkeypatch, prior_cost):
    ref, objects, persistence = snapshots
    p = fast_pipeline
    monkeypatch.setattr(worker, "settings", replace(worker.settings, analysis_pipeline_version=2, analysis_direct_source=True))
    ref.update({"status": "queued", "analysis_settings": worker.analysis_config(), "reserved_milli": 1000,
                "declared_duration_seconds": 20, "analysis_authorization": {"max_minutes": 1},
                "media_metrics": {"performance": {"estimated_compute_usd": prior_cost}}})
    from hosted.app.db import account_ref
    account_ref("alice").set({"available_milli": 0, "reserved_milli": 1000})
    client = checkpoints.storage.client()
    client.download_file = lambda bucket, key, destination: __import__("pathlib").Path(destination).write_bytes(b"video")
    uploads = []
    client.upload_file = lambda source, bucket, key, **kwargs: uploads.append(key)
    monkeypatch.setattr(worker, "ffprobe", cli.ffprobe)
    monkeypatch.setattr(worker, "analysis_copy", lambda *args, **kwargs: pytest.fail("Direct-source analysis must not transcode"))
    data = worker.claim_run("alice", "a7c0c3aa-6af9-4d7c-8c54-2523fd65025b", "owner")
    worker._run_one_claimed("alice", "a7c0c3aa-6af9-4d7c-8c54-2523fd65025b", data, "owner")
    saved = ref.get().to_dict()
    assert saved["status"] == "complete" and len(uploads) == 2
    assert saved["media_metrics"]["transcoded"] is False
    assert saved["media_metrics"]["performance"]["checkpoints"]["bytes"] > 0
    assert saved["media_metrics"]["performance"]["estimated_compute_usd"] > 0
    report = saved["media_metrics"]["performance"]
    rate = worker.settings.worker_cpu * worker.settings.worker_cpu_usd_per_second + worker.settings.worker_memory_gib * worker.settings.worker_memory_usd_per_second
    assert report["estimated_compute_usd"] == pytest.approx(prior_cost + report["elapsed_seconds"] * rate)
    assert prior_cost <= saved["progress"]["estimated_compute_usd"] <= report["estimated_compute_usd"]
    from hosted.app.main import run_summary
    assert run_summary({"id": ref.id, **saved}, admin=True)["performance"]["analysis"]["pipeline_version"] == 2


def test_post_stages_share_multiple_workers(fast_pipeline, monkeypatch):
    import time
    from basketball_highlights import pipeline as module
    p = fast_pipeline
    p.args.max_concurrency = 4
    active, peak = {}, {}
    lock = threading.Lock()
    class Adapter:
        last_attempts = 1
        last_request_id = "request"
        def classify(self, frames):
            center = frames[len(frames)//2].timestamp
            if len(frames) == 5:
                phase, classification = "refinement", "made_basket"
            else:
                phase, classification = "base", "uncertain"
            with lock:
                active[phase] = active.get(phase, 0)+1
                peak[phase] = max(peak.get(phase, 0), active[phase])
            time.sleep(.003)
            with lock:
                active[phase] -= 1
            return Detection(classification=classification, estimated_event_timestamp=center, evidence_timestamps=[center],
                api_response={"answers": [{"name": "outcome", "probabilities": [{"value": "made_basket", "probability": .5}]}]}), {"input_tokens": 100}
        def classify_context(self, frames, timestamp):
            with lock:
                active["context"] = active.get("context", 0)+1
                peak["context"] = max(peak.get("context", 0), active["context"])
            time.sleep(.003)
            with lock:
                active["context"] -= 1
            return ContextResult(classification="game_play", confidence=.99, checked=True), {"input_tokens": 100}
    monkeypatch.setattr(module, "DecisionsAdapter", Adapter)
    cli.analyze(p.args)
    assert peak["refinement"] > 1 and peak["context"] > 1


def test_rate_headers_pace_and_honor_exhausted_token_reset():
    from test_analysis_recovery import Clock
    clock = Clock()
    gate = RequestGate(8, clock=clock.clock, sleep=clock.sleep)
    gate.observe_headers({"x-ratelimit-limit-requests": "60", "x-ratelimit-remaining-tokens": "0",
                          "x-ratelimit-reset-tokens": "1m2s"})
    gate.acquire()
    assert clock.now >= 62
    first = clock.now
    gate.release()
    gate.acquire()
    assert clock.now-first >= 1.25
    gate.release()


def test_delta_payloads_stay_bounded_as_game_grows(fast_pipeline, monkeypatch):
    p = fast_pipeline
    monkeypatch.setattr(pipeline, "ffprobe", lambda *_: {"duration_seconds": 130, "width": 640, "height": 360, "frame_rate": 25})
    payloads = []
    def capture(state, budget):
        payloads.append((state["completed"], state["journal_delta"], len(json.dumps(state)), len(budget["requests"])))
    cli.analyze(p.args, checkpoint_callback=capture)
    deltas = [row for row in payloads if row[1]]
    assert max(row[2] for row in deltas) < 20000
    assert max(row[3] for row in deltas) <= 32
    compactions = [row[0] for row in payloads if not row[1]]
    assert compactions[:3] == [0, 256, 512]


@pytest.mark.parametrize("version", [1, 2])
def test_resume_does_not_refine_uncertain_refinements_again(fast_pipeline, version):
    p = fast_pipeline
    p.args.pipeline_version = version
    def uncertain(frames):
        return Detection(classification="uncertain", estimated_event_timestamp=frames[len(frames)//2].timestamp,
            evidence_timestamps=[frame.timestamp for frame in frames],
            api_response={"answers": [{"name": "outcome", "probabilities": [{"value": "made_basket", "probability": .1}]}]}), {"input_tokens": 100}
    p.adapter.classify = uncertain
    p.adapter.classify_context = lambda *args: (ContextResult(classification="uncertain", checked=True), {"input_tokens": 100})
    cli.analyze(p.args)
    before = json.loads((p.output / "budget.json").read_text())
    p.args.resume = True
    p.adapter.classify = lambda *args: pytest.fail("A saved refinement must not be refined again")
    p.adapter.classify_context = lambda *args: pytest.fail("Saved candidate context must not repeat")
    cli.analyze(p.args)
    after = json.loads((p.output / "budget.json").read_text())
    assert after["request_count"] == before["request_count"]
