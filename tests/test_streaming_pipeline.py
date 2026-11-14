"""Evidence parity, admission durability and bounded concurrent pipeline behavior."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_pipeline_performance import fast_pipeline, legacy_pipeline, timestamp_reference
from basketball_highlights import cli, pipeline
from basketball_highlights.models import Detection, ContextResult
from basketball_highlights.performance import Performance
from basketball_highlights.retry import RequestFailed
from basketball_highlights.scheduling import prepare_jobs
from basketball_highlights.video import FrameReader, SampledFrame, ffprobe
from hosted import worker
from hosted.preview_uploads import PreviewUploads


def enable(args, rolling=True):
    args.prefetch_requests = 4
    args.rolling_scheduler = rolling
    args.chronological_evidence = True
    args.decoder_thread_type = "AUTO"
    args.decoder_threads = 2


@pytest.mark.parametrize("rolling", [False, True])
def test_replay_preserves_events_and_budget(fast_pipeline, tmp_path, rolling):
    p = fast_pipeline
    # Deterministic classifier keyed by evidence, rather than completion order.
    def classify(frames):
        center = frames[len(frames)//2].timestamp
        return Detection(classification="made_basket", estimated_event_timestamp=center,
                         evidence_timestamps=[frame.timestamp for frame in frames], model_confidence=.99), {"input_tokens": 100}
    p.adapter.classify = classify
    p.adapter.classify_context = lambda *args: (ContextResult(classification="game_play", confidence=.99, checked=True), {"input_tokens": 100})
    cli.analyze(p.args)
    expected = json.loads((p.output / "events.json").read_text())
    old_budget = json.loads((p.output / "budget.json").read_text())
    enable(p.args, rolling)
    p.args.output_dir = tmp_path / "optimized"
    cli.analyze(p.args)
    assert json.loads((p.args.output_dir / "events.json").read_text()) == expected
    new_budget = json.loads((p.args.output_dir / "budget.json").read_text())
    for field in ("request_count", "input_tokens", "image_count", "actual_spend_usd", "estimated_spend_usd"):
        assert new_budget[field] == pytest.approx(old_budget[field])
    assert not new_budget["pending"]
    p.args.resume = True
    p.adapter.classify = lambda *args: pytest.fail("Resume must not repeat requests")
    cli.analyze(p.args)
    p.args.prefetch_requests = 2
    with pytest.raises(ValueError, match="Resume settings"):
        cli.analyze(p.args)


@pytest.mark.parametrize("rolling", [False, True])
def test_publish_failure_sends_nothing(fast_pipeline, rolling):
    p = fast_pipeline
    enable(p.args, rolling)
    def publish(state, ledger):
        if ledger["pending"]:
            raise RuntimeError("checkpoint unavailable")
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        cli.analyze(p.args, checkpoint_callback=publish)
    assert p.adapter.outcomes == 0
    assert not json.loads((p.output / "budget.json").read_text())["pending"]


def test_refinement_preparation_is_chronological_without_reordering_requests():
    entries = [({"kind": "refinement", "key": "later", "samples": [4, 5]}, None, 2),
               ({"kind": "refinement", "key": "earlier", "samples": [1, 4]}, None, 2)]
    seen = []
    def frame(t, *_):
        seen.append(t)
        return SampledFrame(t, str(t).encode())
    prepared = prepare_jobs(entries, frame, "refinement", True)
    assert seen == [1, 4, 5]
    assert [entry[0][0]["key"] for entry in prepared] == ["later", "earlier"]
    assert [[f.timestamp for f in value[1]] for value in prepared] == [[4, 5], [1, 4]]


def test_preparation_failure_is_scoped_to_affected_requests():
    entries = [({"kind": "context", "samples": [1, 2]}, None, 2),
               ({"kind": "context", "samples": [3, 4]}, None, 2)]
    def frame(t, *_):
        if t == 2:
            raise RuntimeError("decode failed")
        return SampledFrame(t, b"jpeg")
    prepared = prepare_jobs(entries, frame, "context", True)
    assert isinstance(prepared[0][2], RuntimeError)
    assert [f.timestamp for f in prepared[1][1]] == [3, 4]


def test_prefetch_overlaps_model_and_decoder_on_one_owner_thread(fast_pipeline, monkeypatch):
    p = fast_pipeline
    enable(p.args)
    p.args.prefetch_requests = 1
    p.args.max_concurrency = 1
    started, decoded_next = threading.Event(), threading.Event()
    owners = set()
    class Reader:
        def __init__(self, *args, **kwargs):
            owners.add(threading.get_ident())
        def sample(self, timestamp):
            owners.add(threading.get_ident())
            if timestamp >= .75:
                assert started.wait(2)
                decoded_next.set()
            return SampledFrame(timestamp, b"jpeg")
        def close(self):
            owners.add(threading.get_ident())
    monkeypatch.setattr(pipeline, "FrameReader", Reader)
    def classify(frames):
        started.set()
        assert decoded_next.wait(2), "Next request should decode while this request waits"
        return Detection(classification="missed_shot", evidence_timestamps=[f.timestamp for f in frames]), {"input_tokens": 100}
    p.adapter.classify = classify
    cli.analyze(p.args)
    assert len(owners) == 1 and threading.get_ident() not in owners
    report = json.loads((p.output / "performance.json").read_text())
    assert report["gauges"]["prepared_queue_depth_peak"] <= 1
    assert report["gauges"]["outstanding_requests_peak"] <= 32


def test_sent_failure_drains_and_resume_skips_settled_requests(fast_pipeline):
    p = fast_pipeline
    enable(p.args)
    seen = []
    def classify(frames):
        timestamp = frames[0].timestamp
        seen.append(timestamp)
        if timestamp == .5:
            raise RequestFailed("temporary_api_error", True, 503)
        return Detection(classification="missed_shot", evidence_timestamps=[f.timestamp for f in frames]), {"input_tokens": 100}
    p.adapter.classify = classify
    with pytest.raises(RequestFailed):
        cli.analyze(p.args)
    saved = json.loads((p.output / "run_state.json").read_text())["detections"]
    saved_times = {value["evidence_timestamps"][0] for value in saved.values()}
    assert not json.loads((p.output / "budget.json").read_text())["pending"]
    p.args.resume = True
    resumed = []
    def success(frames):
        resumed.append(frames[0].timestamp)
        return Detection(classification="missed_shot"), {"input_tokens": 100}
    p.adapter.classify = success
    cli.analyze(p.args)
    assert not saved_times.intersection(resumed)


def test_rolling_refills_before_slowest_previous_batch_finishes(fast_pipeline):
    p = fast_pipeline
    enable(p.args)
    p.args.max_concurrency = 2
    next_batch_started = threading.Event()
    pending_sizes = []
    def checkpoint(state, budget):
        pending_sizes.append(len(budget["pending"]))
    def classify(frames):
        timestamp = frames[0].timestamp
        if timestamp >= 8:
            next_batch_started.set()
        if timestamp == 0:
            assert next_batch_started.wait(3), "Rolling admission must not wait for the batch straggler"
        return Detection(classification="missed_shot"), {"input_tokens": 100}
    p.adapter.classify = classify
    cli.analyze(p.args, checkpoint_callback=checkpoint)
    assert next_batch_started.is_set() and max(pending_sizes) <= 32


def test_cancelled_optimized_coordinator_keeps_conservative_sent_reservations(fast_pipeline):
    p = fast_pipeline
    enable(p.args)
    def progress(value):
        if value.get("active_requests"):
            raise worker.AnalysisCancelled()
    # First progress is normally emitted before preparation; remove throttling.
    original = pipeline.time.monotonic
    clock = [0]
    def advancing():
        clock[0] += 6
        return original()+clock[0]
    from unittest.mock import patch
    with patch.object(pipeline.time, "monotonic", advancing):
        with pytest.raises(worker.AnalysisCancelled):
            cli.analyze(p.args, progress_callback=progress)
    budget = json.loads((p.output / "budget.json").read_text())
    assert len(budget["pending"]) <= p.args.max_concurrency
    assert (p.output / "performance.json").exists()


@pytest.mark.parametrize("kind", ["base", "refinement", "context"])
def test_budget_exhaustion_leaves_no_unsent_reservations(fast_pipeline, monkeypatch, kind):
    from basketball_highlights.budget import Budget, BudgetExceeded
    p = fast_pipeline
    enable(p.args)
    original = Budget.reserve
    def reserve(self, *args, **kwargs):
        phase = ("refinement" if kwargs.get("ceiling_fraction") == .9 else
                 "context" if kwargs.get("refinement") else "base")
        if phase == kind:
            raise BudgetExceeded("forced exhaustion")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Budget, "reserve", reserve)
    if kind == "base":
        with pytest.raises(BudgetExceeded):
            cli.analyze(p.args)
    else:
        cli.analyze(p.args)
    assert not json.loads((p.output / "budget.json").read_text())["pending"]


def test_new_settings_pin_and_old_runs_keep_legacy_execution(monkeypatch):
    from hosted.app.config import analysis_settings
    initial = analysis_settings(worker.settings)
    assert initial["prefetch_requests"] == 4 and initial["decoder_thread_type"] == "AUTO"
    # Recovery passes saved settings directly to Args; absent fields retain defaults.
    old = {key: value for key, value in initial.items() if key not in (
        "prefetch_requests", "rolling_scheduler", "chronological_evidence", "decoder_thread_type",
        "decoder_threads", "overlap_preview_uploads")}
    args = SimpleNamespace(**old)
    assert getattr(args, "prefetch_requests", 0) == 0
    assert getattr(args, "decoder_thread_type", "SLICE") == "SLICE"
    with pytest.raises(ValueError):
        analysis_settings(replace(worker.settings, analysis_rolling_scheduler=True, analysis_prefetch_requests=0))


def test_upload_queue_is_bounded_and_failures_propagate():
    from basketball_highlights.models import Event
    event = Event(event_id="basket_001", timestamp_seconds=1, clip_start_seconds=0, clip_end_seconds=2,
                  classification="made_basket", shot_type="unknown", model_confidence=.9,
                  needs_review=False, evidence_timestamps=[1], source_windows=["w1"])
    def fail(*args):
        raise RuntimeError("upload failed")
    uploads = PreviewUploads(fail, Performance())
    uploads.submit(event, Path("clip.mp4"))
    with pytest.raises(RuntimeError, match="upload failed"):
        uploads.finish()
    uploads.finish()  # Cleanup after an upload failure must be idempotent.


def test_previews_upload_before_all_clips_render_and_local_files_can_be_removed(fast_pipeline, monkeypatch):
    p = fast_pipeline
    enable(p.args)
    uploaded = []
    upload_started = threading.Event()
    original = cli.create_clip
    created = []
    def render(*args, **kwargs):
        if len(created) >= 2:
            assert upload_started.wait(2), "Uploads should start before rendering every preview"
        original(*args, **kwargs)
        created.append(args[1])
    monkeypatch.setattr(cli, "create_clip", render)
    def classify(frames):
        center = frames[len(frames)//2].timestamp
        return Detection(classification="made_basket", estimated_event_timestamp=center,
                         evidence_timestamps=[center]), {"input_tokens": 100}
    p.adapter.classify = classify
    p.adapter.classify_context = lambda *args: (ContextResult(classification="game_play", confidence=.99, checked=True), {"input_tokens": 100})
    def upload(event, path, remove):
        upload_started.set()
        assert path.is_file()
        size = path.stat().st_size
        if remove:
            path.unlink()
        uploaded.append(event.event_id)
        return (event, "private/"+event.event_id, event.model_dump()), size
    perf = Performance()
    uploads = PreviewUploads(upload, perf)
    p.args.media_ready = uploads.submit
    p.args.create_reel_output = False
    try:
        cli.analyze(p.args)
        uploads.finish()
    finally:
        uploads.finish()
    assert len(uploaded) == len(created) > 2
    assert all(not path.exists() for path in created)
    assert perf.report()["gauges"]["preview_upload_queue_depth_peak"] <= 4


def test_each_pass_reports_separate_counts_without_changing_footage_completion(fast_pipeline):
    p = fast_pipeline
    enable(p.args)
    def classify(frames):
        center = frames[len(frames)//2].timestamp
        return Detection(classification="uncertain" if len(frames) < 5 else "made_basket",
            estimated_event_timestamp=center, evidence_timestamps=[center],
            api_response={"answers": [{"name": "outcome", "probabilities": [{"value": "made_basket", "probability": .5}]}]}), {"input_tokens": 100}
    p.adapter.classify = classify
    p.adapter.classify_context = lambda *args: (ContextResult(classification="game_play", confidence=.99, checked=True), {"input_tokens": 100})
    values = []
    cli.analyze(p.args, progress_callback=values.append)
    phases = [value["phase"] for value in values]
    for phase in ("analyzing", "refining_candidates", "checking_game_context", "creating_clips"):
        assert phase in phases
    for phase in ("refining_candidates", "checking_game_context"):
        reports = [value for value in values if value["phase"] == phase]
        assert reports[0]["stage_completed"] == 0
        assert reports[-1]["stage_completed"] == reports[-1]["stage_total"] > 0
        assert all(value["completed"] == value["total"] for value in reports)
    clips = [value for value in values if value["phase"] == "creating_clips"]
    assert clips[0]["clips_completed"] == 0 and clips[0]["clips_total"] > 0
    assert all(value["stage_total"] == value["clips_total"] for value in clips)


@pytest.mark.parametrize("threads", [2, 4])
def test_auto_threading_preserves_exact_pts_and_jpeg(tmp_path, threads):
    import subprocess
    source = tmp_path / "bframes.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=4",
                    "-vf", "select='if(lt(t,2),not(mod(n,2)),1)'", "-fps_mode", "vfr", "-c:v", "libx264",
                    "-bf", "3", str(source)], check=True)
    info = ffprobe(source)
    expected = timestamp_reference(source, info)
    readers = [FrameReader(source, info=info, decoder_thread_type=kind, decoder_threads=threads)
               for kind in ("SLICE", "AUTO")]
    try:
        for timestamp in (0, .25, 1.125, 2.25, 3.5, .75, 3, 3.75):
            frames = [reader.sample(timestamp) for reader in readers]
            pts, _ = expected(timestamp)
            assert all(reader.timestamp_reader.selected_time == pytest.approx(pts, abs=1e-6) for reader in readers)
            assert frames[0].jpeg == frames[1].jpeg
    finally:
        for reader in readers:
            reader.close()
