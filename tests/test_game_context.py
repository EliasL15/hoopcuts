"""Context filtering behavior with isolated model and video doubles."""
from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import pytest

from test_billing import env as hosted_env

from basketball_highlights import cli, csv_highlights, decisions
from basketball_highlights.budget import Budget, BudgetExceeded
from basketball_highlights.decisions import DecisionsAdapter
from basketball_highlights.events import merge_detections
from basketball_highlights.game_context import apply_context, candidate_key, context_times
from basketball_highlights.models import ContextResult, Detection, Event
from basketball_highlights.video import SampledFrame


def context_answer(choice="game_play", confidence=.95):
    return {"answers": [{"type": "choice", "name": "game_context", "choice": choice,
                         "confidence": confidence}], "usage": {"input_tokens": 100}}


def adapter_with_responses(monkeypatch, responses, retries=1):
    adapter = DecisionsAdapter.__new__(DecisionsAdapter)
    requests = []
    responses = iter(responses)
    def create(**kwargs):
        requests.append(kwargs)
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(id=f"request-{len(requests)}", model_dump=lambda: value)
    adapter.client = SimpleNamespace(decisions=SimpleNamespace(create=create))
    adapter.retries = retries
    monkeypatch.setattr(decisions.time, "sleep", lambda *_: None)
    return adapter, requests


@pytest.mark.parametrize("choice", ["game_play", "warmup_or_break", "uncertain"])
def test_context_choices_and_request_evidence(monkeypatch, choice):
    adapter, requests = adapter_with_responses(monkeypatch, [context_answer(choice)])
    frames = [SampledFrame(t, b"jpeg") for t in [2, 5, 10]]
    result, usage = adapter.classify_context(frames, 5)
    assert result.classification == choice and result.checked
    assert usage == {"input_tokens": 100}
    assert adapter.last_attempts == 1 and adapter.last_request_id == "request-1"
    request = requests[0]
    assert request["model"] == "gpt-6-luna"
    assert len([part for part in request["input"][0]["content"] if part["type"] == "input_image"]) == 3
    text = request["input"][0]["content"][0]["text"]
    assert "5.000 seconds" in text and "2.000, 5.000, 10.000" in text
    instructions = request["questions"][0]["instructions"]
    for term in ["halftime", "pregame", "between-period", "postgame", "free throws", "buzzer-beaters", "stopped clock"]:
        assert term in instructions


@pytest.mark.parametrize("confidence", [None, .89, "0.99", True, float("nan"), float("inf"), -1, 1.1])
def test_untrusted_confidence_cannot_hide_candidate(monkeypatch, confidence):
    adapter, _ = adapter_with_responses(monkeypatch, [context_answer("warmup_or_break", confidence)])
    result, usage = adapter.classify_context([SampledFrame(1, b"jpeg")], 1)
    assert result.classification == "uncertain" and result.checked
    assert usage["input_tokens"] == 100


@pytest.mark.parametrize("answers", [None, [], [None], [{"name": "game_context", "type": "score", "choice": "warmup_or_break", "confidence": 1}],
                                        [{"name": "game_context", "type": "choice", "choice": "other", "confidence": 1}]])
def test_malformed_context_answers_are_uncertain(monkeypatch, answers):
    adapter, _ = adapter_with_responses(monkeypatch, [{"answers": answers}])
    result, _ = adapter.classify_context([SampledFrame(1, b"jpeg")], 1)
    assert result.classification == "uncertain" and result.checked


def test_context_retry_and_terminal_failure(monkeypatch):
    adapter, requests = adapter_with_responses(monkeypatch, [RuntimeError("temporary"), context_answer()])
    result, _ = adapter.classify_context([SampledFrame(1, b"jpeg")], 1)
    assert result.classification == "game_play" and adapter.last_attempts == 2
    assert len(requests) == 2
    adapter, requests = adapter_with_responses(monkeypatch, [RuntimeError("failed"), RuntimeError("failed")])
    with pytest.raises(RuntimeError, match="Decisions request failed"):
        adapter.classify_context([SampledFrame(1, b"jpeg")], 1)
    assert adapter.last_attempts == len(requests) == 2


def test_context_threshold_is_inclusive():
    assert DecisionsAdapter._parse_context(context_answer("warmup_or_break", .90)).classification == "warmup_or_break"


def test_context_sampling_boundaries_and_identity():
    assert context_times(15, 30, 30) == [5, 9, 12, 15, 18, 21, 25]
    start = context_times(0, 2, 25)
    assert start == [0, 1.96]
    end = context_times(2, 2, 25)
    assert end == [0, 1.96]
    assert context_times(0, .01, 25) == [0]
    assert max(context_times(30, 30)) < 30
    assert candidate_key({"source_windows": ["w2", "w1"]}) == candidate_key({"source_windows": ["w1", "w2"]})


def test_context_preserves_basket_outcome_timestamp_and_evidence():
    detections = [("w1", Detection(classification="made_basket", shot_type="free_throw",
                    estimated_event_timestamp=5, evidence_timestamps=[4.75, 5], model_confidence=.98)),
                  ("w2", Detection(classification="made_basket", shot_type="free_throw",
                    estimated_event_timestamp=5.25, evidence_timestamps=[5, 5.25], model_confidence=.95))]
    candidates = merge_detections(detections)
    assert len(candidates) == 1
    key = candidate_key(candidates[0])
    original = candidates[0]
    kept, counts = apply_context(candidates, {key: ContextResult(classification="game_play", confidence=.95, checked=True)})
    assert kept[0]["timestamp"] == 5 and kept[0]["evidence_timestamps"] == original["evidence_timestamps"]
    assert kept[0]["included"] and not kept[0]["needs_review"]
    assert kept[0]["shot_type"] == "free_throw"
    assert counts == {"checked": 1, "excluded": 0, "unchecked": 0}
    kept, counts = apply_context(candidates, {key: ContextResult(classification="warmup_or_break", confidence=.95, checked=True)})
    assert kept == [] and counts["excluded"] == 1
    for results in [{}, {key: ContextResult(classification="warmup_or_break", confidence=.5, checked=True)}]:
        kept, _ = apply_context(candidates, results)
        assert kept[0]["classification"] == "made_basket"
        assert kept[0]["game_context"] == "uncertain" and kept[0]["needs_review"] and not kept[0]["included"]


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"fake-video")
    output = tmp_path / "output"
    args = cli.parser().parse_args(["analyze", str(video), "--output-dir", str(output), "--max-concurrency", "1", "--pipeline-version", "1"])
    # This fixture deliberately exercises the original algorithm; tests opting
    # into candidate refinement set their own configuration explicitly.
    args.candidate_refinement = False
    args.refinement_frames = 5
    args.rescue_threshold = .05
    args.rescue_missed = False
    args.rescue_max_requests = 16
    sampled = []
    def sample(_video, timestamp, *_args):
        sampled.append(timestamp)
        return SampledFrame(timestamp, b"jpeg")
    monkeypatch.setattr(cli, "ffprobe", lambda *_: {"duration_seconds": 20, "width": 640, "height": 360, "frame_rate": 25})
    monkeypatch.setattr(cli, "sample_frame", sample)
    clips, reels = [], []
    def create_clip(_source, target, start, end):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip")
        clips.append((target.name, start, end))
    monkeypatch.setattr(cli, "create_clip", create_clip)
    monkeypatch.setattr(cli, "create_reel", lambda paths, _target: reels.append([path.name for path in paths]))
    class Adapter:
        retries = 1
        last_attempts = 1
        last_request_id = "fake-request"
        outcomes = 0
        checks = []
        failure = False
        def classify(self, frames):
            self.outcomes += 1
            center = 2 if frames[0].timestamp < 6 else 8 if frames[0].timestamp < 12 else 15
            return Detection(classification="made_basket", shot_type="free_throw" if center == 2 else "two_pointer",
                estimated_event_timestamp=center, evidence_timestamps=[center], model_confidence=.98), {"input_tokens": 100}
        def classify_context(self, frames, timestamp):
            self.checks.append((timestamp, [frame.timestamp for frame in frames]))
            if self.failure:
                self.last_attempts = 2
                raise RuntimeError("simulated API failure")
            choice = {2: "game_play", 8: "warmup_or_break", 15: "uncertain"}[timestamp]
            return ContextResult(classification=choice, confidence=.95, checked=True), {"input_tokens": 100}
    adapter = Adapter()
    monkeypatch.setattr(cli, "DecisionsAdapter", lambda: adapter)
    return SimpleNamespace(args=args, output=output, adapter=adapter, sampled=sampled, clips=clips, reels=reels)


def test_pipeline_filters_once_after_merging_and_resumes(pipeline):
    p = pipeline
    progress = []
    assert cli.analyze(p.args, progress_callback=progress.append) == 0
    events = json.loads((p.output / "events.json").read_text())
    assert [event["timestamp_seconds"] for event in events] == [2, 15]
    assert events[0]["included"] and events[0]["shot_type"] == "free_throw"
    assert events[1]["game_context"] == "uncertain" and events[1]["needs_review"] and not events[1]["included"]
    assert len(p.adapter.checks) == 3 and len(p.clips) == 2
    assert p.reels == [["basket_001.mp4"]]
    summary = json.loads((p.output / "processing_summary.json").read_text())
    assert summary["game_context"] == {"checked": 3, "excluded": 1, "unchecked": 0}
    assert summary["complete"] and summary["budget"]["pending"] == []
    assert all(item["completed"] == item["total"] for item in progress if item["phase"] == "checking_game_context")
    checkpoint = json.loads((p.output / "run_state.json").read_text())
    assert len(checkpoint["contexts"]) == 3
    outcome_calls = p.adapter.outcomes
    p.args.resume = True
    assert cli.analyze(p.args) == 0
    assert p.adapter.outcomes == outcome_calls and len(p.adapter.checks) == 3
    checkpoint["signature"].pop("analysis_version")
    (p.output / "run_state.json").write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="Resume settings"):
        cli.analyze(p.args)


def test_context_failures_settle_retries_and_keep_unselected(pipeline):
    p = pipeline
    p.adapter.failure = True
    cli.analyze(p.args)
    events = json.loads((p.output / "events.json").read_text())
    assert len(events) == 3 and all(event["needs_review"] and not event["included"] for event in events)
    summary = json.loads((p.output / "processing_summary.json").read_text())
    assert summary["game_context"] == {"checked": 0, "excluded": 0, "unchecked": 3}
    assert len(summary["errors"]) == 3 and summary["budget"]["pending"] == []
    failed = [entry for entry in summary["budget"]["requests"] if entry["error"]]
    assert len(failed) == 3 and all(entry["attempts"] == 2 for entry in failed)
    assert p.reels == []


def test_context_budget_exhaustion_does_not_hide_baskets(pipeline, monkeypatch):
    original = Budget.reserve
    def reserve(budget, *args, **kwargs):
        if kwargs.get("refinement") and kwargs.get("ceiling_fraction") is None:
            raise BudgetExceeded("context allowance exhausted")
        return original(budget, *args, **kwargs)
    monkeypatch.setattr(Budget, "reserve", reserve)
    cli.analyze(pipeline.args)
    assert pipeline.adapter.checks == []
    events = json.loads((pipeline.output / "events.json").read_text())
    assert len(events) == 3 and all(event["needs_review"] and not event["included"] for event in events)
    assert json.loads((pipeline.output / "processing_summary.json").read_text())["game_context"]["unchecked"] == 3


def test_budget_phase_ceilings_and_unused_sampling_reservation(tmp_path):
    budget = Budget(tmp_path / "budget.json", .001)
    reserve = budget.reserve(1)
    budget.settle(reserve, input_tokens=7950, output_tokens=0, images=1, request_id="base")
    with pytest.raises(BudgetExceeded):
        budget.reserve(0)
    with pytest.raises(BudgetExceeded):
        budget.reserve(7, refinement=True, ceiling_fraction=.9)
    reserve = budget.reserve(1, refinement=True, ceiling_fraction=.9)
    budget.settle(reserve, input_tokens=1000, output_tokens=0, images=1, request_id="refine")
    reserve = budget.reserve(1, refinement=True)
    before = budget.data["request_count"]
    budget.release(reserve)
    assert budget.data["request_count"] == before and budget.data["pending"] == []
    with pytest.raises(BudgetExceeded):
        budget.reserve(7, refinement=True)


def test_historical_event_and_csv_selection(tmp_path, monkeypatch):
    event = Event(event_id="e1", timestamp_seconds=5, clip_start_seconds=2, clip_end_seconds=8,
                  classification="made_basket", needs_review=False, evidence_timestamps=[5])
    assert event.game_context is None and event.included
    monkeypatch.setattr(csv_highlights, "ffprobe", lambda *_: {"duration_seconds": 30})
    clips, reels = [], []
    monkeypatch.setattr(csv_highlights, "create_clip", lambda _video, target, start, end: clips.append((start, end)))
    monkeypatch.setattr(csv_highlights, "create_reel", lambda paths, _target: reels.append(len(paths)))
    video = tmp_path / "video.mp4"
    video.touch()
    csv_path = tmp_path / "events.csv"
    for explicit in [True, False]:
        fields = ["timestamp_seconds", "classification", "needs_review"] + (["included"] if explicit else [])
        with csv_path.open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for timestamp, selected in [(5, True), (15, False)]:
                row = {"timestamp_seconds": timestamp, "classification": "made_basket", "needs_review": True}
                if explicit:
                    row["included"] = selected
                writer.writerow(row)
        paths = csv_highlights.generate_from_csv(video, csv_path, tmp_path / str(explicit))
        assert len(paths) == (1 if explicit else 2)
    assert reels == [1, 2]


def test_interrupted_context_stage_resumes_without_repeating_completed_check(pipeline):
    class Cancelled(RuntimeError):
        pass
    def progress(value):
        if value["phase"] == "checking_game_context":
            raise Cancelled()
    with pytest.raises(Cancelled):
        cli.analyze(pipeline.args, progress_callback=progress)
    assert len(pipeline.adapter.checks) == 1
    events = json.loads((pipeline.output / "events.json").read_text())
    assert len(events) == 3 and not events[-1]["included"]
    pipeline.args.resume = True
    cli.analyze(pipeline.args)
    assert [check[0] for check in pipeline.adapter.checks] == [2, 8, 15]
    assert len(json.loads((pipeline.output / "events.json").read_text())) == 2


def test_context_sampling_failure_releases_unused_reserve(pipeline, monkeypatch):
    original = cli.sample_frame
    def sample(video, timestamp, *args):
        # This time is sampled only by the wider check, never by the base pass.
        if timestamp == pytest.approx(19.96):
            raise RuntimeError("cannot decode final context frame")
        return original(video, timestamp, *args)
    monkeypatch.setattr(cli, "sample_frame", sample)
    cli.analyze(pipeline.args)
    summary = json.loads((pipeline.output / "processing_summary.json").read_text())
    assert summary["game_context"]["unchecked"] == 1
    assert summary["budget"]["pending"] == []
    assert all(not row["error"] for row in summary["budget"]["requests"])
    assert summary["budget"]["request_count"] == pipeline.adapter.outcomes + len(pipeline.adapter.checks)


def test_context_evaluation_reports_real_game_false_exclusions(tmp_path):
    from basketball_highlights.evaluate import evaluate_context
    candidates = [{"timestamp": i, "source_windows": [f"w{i}"]} for i in [5, 10, 15, 20]]
    contexts = {candidate_key(candidate): ContextResult(classification="warmup_or_break", confidence=.95, checked=True)
                for candidate in candidates[:2]}
    labels = tmp_path / "labels.csv"
    labels.write_text("timestamp_seconds,game_context\n5,warmup_or_break\n10,game_play\n15,game_play\n30,game_play\n")
    report = evaluate_context(candidates, contexts, str(labels))
    assert report["warmup_baskets_excluded"] == 1
    assert report["game_baskets_incorrectly_excluded"] == 1
    assert report["game_baskets_retained_for_review"] == 1
    assert report["matched_candidates"] == 3 and report["unlabeled_candidates"] == 1
    assert report["labels"][-1]["decision"] == "no_candidate"


def test_unclear_context_manual_selection_roundtrip(tmp_path):
    from fastapi.testclient import TestClient
    from basketball_highlights import web
    app = web.create_app(tmp_path)
    directory = tmp_path / "a1111111-1111-4111-8111-111111111111"
    directory.mkdir()
    (directory / "source.mp4").write_bytes(b"source")
    (directory / "project.json").write_text(json.dumps({"id": "a1111111-1111-4111-8111-111111111111", "name": "Game", "video": "source.mp4", "status": "complete"}))
    app.state.store.save_events(directory, [Event(event_id="e1", timestamp_seconds=5, clip_start_seconds=2,
        clip_end_seconds=8, classification="made_basket", needs_review=True, evidence_timestamps=[5],
        included=False, game_context="uncertain")])
    with TestClient(app) as api:
        url = "/api/projects/a1111111-1111-4111-8111-111111111111"
        event = api.get(url).json()["events"][0]
        assert not event["included"] and event["game_context"] == "uncertain"
        assert api.patch(url + "/events/e1", json={"included": True}).status_code == 200
        event = api.get(url).json()["events"][0]
        assert event["included"] and event["game_context"] == "uncertain"
    app.state.store.executor.shutdown(wait=True)


def test_outcome_refinement_precedes_context_with_90_percent_ceiling(pipeline, monkeypatch):
    seen_ceilings = []
    original_reserve = Budget.reserve
    def reserve(budget, *args, **kwargs):
        seen_ceilings.append(kwargs.get("ceiling_fraction"))
        return original_reserve(budget, *args, **kwargs)
    monkeypatch.setattr(Budget, "reserve", reserve)
    def classify(frames):
        if len(frames) == 5:
            return Detection(classification="made_basket", estimated_event_timestamp=5,
                evidence_timestamps=[frame.timestamp for frame in frames]), {"input_tokens": 100}
        if frames[0].timestamp == 0:
            return Detection(classification="uncertain", estimated_event_timestamp=5, evidence_timestamps=[5],
                api_response={"answers": [{"name": "outcome", "probabilities": [{"value": "made_basket", "probability": .5}]}]}), {"input_tokens": 100}
        return Detection(classification="missed_shot", estimated_event_timestamp=frames[0].timestamp), {"input_tokens": 100}
    pipeline.adapter.classify = classify
    checked = []
    def classify_context(frames, timestamp):
        checked.append(timestamp)
        return ContextResult(classification="game_play", confidence=.95, checked=True), {"input_tokens": 100}
    pipeline.adapter.classify_context = classify_context
    cli.analyze(pipeline.args)
    assert .9 in seen_ceilings and checked == [5]
    events = json.loads((pipeline.output / "events.json").read_text())
    assert len(events) == 1 and events[0]["classification"] == "made_basket" and events[0]["included"]
    assert events[0]["timestamp_seconds"] == 5 and all("_refine" in key for key in events[0]["source_windows"])


def test_original_basket_adapter_still_parses_outcome_and_shot(monkeypatch):
    raw = {"answers": [{"name": "outcome", "type": "choice", "choice": "made_basket", "confidence": .97},
                       {"name": "shot_type", "type": "choice", "choice": "free_throw"}],
           "usage": {"input_tokens": 100}}
    adapter, requests = adapter_with_responses(monkeypatch, [raw])
    frames = [SampledFrame(t, b"jpeg") for t in [1, 1.25, 1.5]]
    detection, usage = adapter.classify(frames)
    assert detection.classification == "made_basket" and detection.shot_type == "free_throw"
    assert detection.estimated_event_timestamp == 1.25 and detection.evidence_timestamps == [1, 1.25, 1.5]
    assert usage["input_tokens"] == 100 and requests[0]["questions"][0]["name"] == "outcome"


def test_hosted_context_payload_and_manual_selection_survive_worker_save(hosted_env):
    from fastapi.testclient import TestClient
    from hosted.app import main, db as refs
    from hosted import worker
    from test_media import RUN
    store, _ = hosted_env
    store.batch = lambda: SimpleNamespace(set=lambda ref, row: ref.set(row), commit=lambda: None)
    event = Event(event_id="context-event", timestamp_seconds=15, clip_start_seconds=10, clip_end_seconds=20,
        classification="made_basket", needs_review=True, evidence_timestamps=[15], included=False,
        game_context="uncertain")
    saved = [(event, None, event.model_dump())]
    worker.save_events("alice", RUN, saved)
    row = refs.event_ref("alice", RUN, event.event_id).get().to_dict()
    assert row["game_context"] == "uncertain" and not row["included"]
    with TestClient(main.app) as api:
        url = f"/api/runs/{RUN}"
        events = api.get(url).json()["events"]
        found = next(item for item in events if item["event_id"] == event.event_id)
        assert found["game_context"] == "uncertain" and found["needs_review"] and not found["included"]
        assert api.patch(url + "/events/" + event.event_id, json={"included": True}).status_code == 200
        worker.save_events("alice", RUN, saved)
        found = next(item for item in api.get(url).json()["events"] if item["event_id"] == event.event_id)
        assert found["included"] and found["game_context"] == "uncertain"
