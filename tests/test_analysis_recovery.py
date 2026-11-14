"""Durable hosted recovery and coordinated API failure behavior."""
from __future__ import annotations

import copy
import io
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from basketball_highlights import cli
from basketball_highlights.budget import Budget
from basketball_highlights.retry import RequestFailed, RequestGate, failure
from hosted import checkpoints, worker
from hosted.app import db as refs, tasks
from test_billing import env
from test_game_context import pipeline
from test_media import NOW, RUN


class Clock:
    def __init__(self):
        self.now = 0
    def clock(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def test_rate_limit_cooldown_concurrency_and_recovery():
    clock = Clock()
    gate = RequestGate(4, clock=clock.clock, sleep=clock.sleep)
    gate.failed(RequestFailed("rate_limit", True, 429, 90), 0)
    assert gate.concurrency == 1
    gate.acquire()
    assert clock.now >= 90
    gate.release()
    for _ in range(32):
        gate.succeeded()
    assert gate.concurrency == 2
    for _ in range(96):
        gate.succeeded()
    assert gate.concurrency == 4


@pytest.mark.parametrize("status,code,category,retryable", [
    (429, None, "rate_limit", True), (429, "insufficient_quota", "quota_exhausted", False),
    (401, None, "authentication", False), (403, None, "permission", False),
    (400, None, "invalid_request", False), (503, None, "temporary_api_error", True)])
def test_safe_failure_categories(status, code, category, retryable):
    exc = RuntimeError("private payload must not appear")
    exc.status_code, exc.body = status, {"error": {"code": code}}
    exc.response = SimpleNamespace(headers={"retry-after": "45"})
    result = failure(exc)
    assert (result.category, result.retryable) == (category, retryable)
    assert "private payload" not in str(result)
    if retryable:
        assert result.retry_after == 45


def test_gate_abort_stops_waiting_and_terminal_errors_stop_peers():
    gate = RequestGate(4)
    gate.failed(RequestFailed("quota_exhausted", False), 0)
    with pytest.raises(RequestFailed, match="quota_exhausted"):
        gate.acquire()
    gate.abort(worker.AnalysisCancelled())
    with pytest.raises(worker.AnalysisCancelled):
        gate.acquire()


@pytest.fixture
def snapshots(env, monkeypatch):
    store, ref = env
    objects = {}
    def put_object(**kwargs):
        objects[kwargs["Key"]] = kwargs["Body"]
    def get_object(**kwargs):
        return {"Body": io.BytesIO(objects[kwargs["Key"]])}
    s3 = SimpleNamespace(put_object=put_object, get_object=get_object,
                        head_object=lambda **kw: {"ETag": "source-etag", "ContentLength": 10})
    monkeypatch.setattr(checkpoints.storage, "client", lambda: s3)
    monkeypatch.setattr(checkpoints.storage, "delete_object", lambda key: objects.pop(key, None))
    monkeypatch.setattr(checkpoints, "client", lambda: store)
    monkeypatch.setattr(checkpoints, "server_timestamp", lambda: NOW)
    monkeypatch.setattr(checkpoints, "utcnow", lambda: NOW)
    ref.update({"status": "running", "worker_token": "owner"})
    config = worker.analysis_config()
    persistence = checkpoints.Checkpoints("alice", RUN, "owner", config)
    return ref, objects, persistence


def envelope(tmp_path):
    budget = Budget(tmp_path / "budget.json", 5)
    budget.reserve(3, attempts=4)
    return {"signature": {"duration": 20, "window_seconds": .75, "stride_seconds": .25}, "detections": {}, "contexts": {}, "errors": []}, copy.deepcopy(budget.data)


def test_checkpoint_restore_keeps_pending_cost_and_rejects_corruption(snapshots, tmp_path):
    ref, objects, persistence = snapshots
    state, ledger = envelope(tmp_path)
    identity = persistence.identity()
    persistence.publish(identity, state, ledger)
    output = tmp_path / "new-worker"
    assert persistence.restore(output, identity)
    resumed = Budget(output / "budget.json", 5)
    assert resumed.spend == pytest.approx(.0006)
    assert resumed.data["pending"] == []
    key = ref.get().to_dict()["analysis_checkpoint"]["key"]
    objects[key] = b"corrupted"
    with pytest.raises(checkpoints.CheckpointError):
        persistence.restore(tmp_path / "bad", identity)
    assert not (tmp_path / "bad" / "budget.json").exists()


def test_checkpoint_owner_and_source_fencing(snapshots, tmp_path):
    ref, objects, persistence = snapshots
    state, ledger = envelope(tmp_path)
    identity = persistence.identity()
    persistence.publish(identity, state, ledger)
    pointer = ref.get().to_dict()["analysis_checkpoint"]
    with pytest.raises(checkpoints.CheckpointError):
        persistence.restore(tmp_path / "other", {**identity, "etag": "changed"})
    ref.update({"worker_token": "new-owner"})
    with pytest.raises(worker.WorkerLeaseLost):
        persistence.publish(identity, state, ledger)
    assert ref.get().to_dict()["analysis_checkpoint"] == pointer
    with pytest.raises(worker.WorkerLeaseLost):
        worker.owned_update("alice", RUN, "owner", {"media_metrics": {}})


def test_checkpoint_storage_failure_stops_before_model(pipeline):
    def unavailable(*args):
        raise checkpoints.CheckpointError("storage unavailable")
    with pytest.raises(checkpoints.CheckpointError):
        cli.analyze(pipeline.args, checkpoint_callback=unavailable)
    assert pipeline.adapter.outcomes == 0


def test_failed_window_is_unfinished_and_resume_uses_stable_identity(pipeline, tmp_path):
    p = pipeline
    good = p.adapter.classify
    original_calls = []
    def fail_once(frames):
        original_calls.append(frames[0].timestamp)
        if len(original_calls) == 3:
            raise RequestFailed("rate_limit", True, 429)
        return good(frames)
    p.adapter.classify = fail_once
    durable = []
    identity = {"run_id": RUN, "etag": "fixed"}
    progress = []
    with pytest.raises(RequestFailed):
        cli.analyze(p.args, progress_callback=progress.append,
                    checkpoint_callback=lambda state, ledger: durable.append(copy.deepcopy((state, ledger))),
                    source_identity=identity)
    state, ledger = durable[-1]
    assert len(state["detections"]) == 2
    assert progress[-1]["completed"] == 2 and progress[-1]["phase"] != "complete"
    assert not json.loads((p.output / "processing_summary.json").read_text())["complete"]
    new_source = tmp_path / "different-path.mp4"
    new_source.write_bytes(b"normalized-again")
    new_output = tmp_path / "resumed"
    new_output.mkdir()
    (new_output / "run_state.json").write_text(json.dumps(state))
    (new_output / "budget.json").write_text(json.dumps(ledger))
    p.args.video, p.args.output_dir, p.args.resume = new_source, new_output, True
    resumed_calls = []
    def resumed(frames):
        resumed_calls.append(frames[0].timestamp)
        return good(frames)
    p.adapter.classify = resumed
    assert cli.analyze(p.args, source_identity=identity) == 0
    assert not set(original_calls[:2]) & set(resumed_calls)
    summary = json.loads((new_output / "processing_summary.json").read_text())
    assert summary["complete"]
    assert summary["budget"]["estimated_spend_usd"] >= ledger["estimated_spend_usd"]


def test_recovery_preserves_reservation_and_has_fixed_limit(env, monkeypatch):
    monkeypatch.setattr(tasks, "settings", replace(tasks.settings,
        gcp_project_id="test-project", dispatch_url="https://dispatcher.invalid",
        dispatch_service_account="dispatcher@example.invalid", internal_audience="https://dispatcher.invalid"))
    store, ref = env
    account = refs.account_ref("alice")
    account.set({"available_milli": 0, "reserved_milli": 60000})
    ref.update({"status": "running", "worker_token": "owner", "reserved_milli": 60000,
                "declared_duration_seconds": 3600, "progress": {"completed": 5, "total": 10}})
    calls = []
    monkeypatch.setattr(tasks, "enqueue", lambda *args: calls.append(args))
    for count in range(3):
        assert worker.recover_run("alice", RUN, "owner")
        assert ref.get().to_dict()["recovery_count"] == count + 1
        assert account.get().to_dict()["reserved_milli"] == 60000
        ref.update({"status": "running", "worker_token": "owner"})
    assert worker.recover_run("alice", RUN, "owner")
    assert ref.get().to_dict()["status"] == "failed"
    assert account.get().to_dict()["reserved_milli"] == 0
    assert len(calls) == 3
    assert not worker.recover_run("alice", RUN, "owner")


def test_expired_cancel_settles_once_and_does_not_dispatch(env, monkeypatch):
    store, ref = env
    account = refs.account_ref("alice")
    account.set({"available_milli": 0, "reserved_milli": 60000})
    ref.update({"status": "cancel_requested", "worker_token": "owner", "reserved_milli": 60000,
                "declared_duration_seconds": 3600, "lease_expires_at": NOW - timedelta(minutes=1),
                "progress": {"completed": 5, "total": 10}})
    monkeypatch.setattr(tasks, "enqueue", lambda *args: pytest.fail("Cancellation must not dispatch"))
    assert worker.recover_run("alice", RUN, "owner", expired=True)
    assert ref.get().to_dict()["status"] == "cancelled"
    assert account.get().to_dict()["reserved_milli"] == 0
    assert not worker.recover_run("alice", RUN, "owner", expired=True)


def test_four_attempts_and_shared_gate_apply_to_context(monkeypatch):
    from basketball_highlights.decisions import DecisionsAdapter
    from basketball_highlights.video import SampledFrame
    clock = Clock()
    gate = RequestGate(4, clock=clock.clock, sleep=clock.sleep)
    adapter = object.__new__(DecisionsAdapter)
    adapter.retries, adapter.gate = 3, gate
    attempts = []
    def create(**kwargs):
        attempts.append(clock.now)
        error = RuntimeError("redacted")
        error.status_code = 429
        error.response = SimpleNamespace(headers={"retry-after": "10"})
        raise error
    adapter.client = SimpleNamespace(decisions=SimpleNamespace(create=create))
    with pytest.raises(RequestFailed, match="rate_limit"):
        adapter.classify_context([SampledFrame(1, b"jpeg")], 1)
    assert adapter.last_attempts == len(attempts) == 4
    assert attempts[1] - attempts[0] >= 10
    assert attempts[2] - attempts[1] >= 10
    assert attempts[3] - attempts[2] >= 20
    assert gate.concurrency == 1


def test_resumed_output_matches_uninterrupted_game(pipeline, tmp_path):
    p = pipeline
    identity = {"run_id": RUN, "etag": "fixed"}
    latest = []
    def capture(state, ledger):
        latest[:] = [copy.deepcopy(state), copy.deepcopy(ledger)]
    assert cli.analyze(p.args, checkpoint_callback=capture, source_identity=identity) == 0
    expected = json.loads((p.output / "events.json").read_text())
    output = tmp_path / "recovered-worker"
    output.mkdir()
    (output / "run_state.json").write_text(json.dumps(latest[0]))
    (output / "budget.json").write_text(json.dumps(latest[1]))
    p.args.output_dir, p.args.resume = output, True
    p.adapter.classify = lambda *args: pytest.fail("A completed request must not repeat")
    p.adapter.classify_context = lambda *args: pytest.fail("A completed context check must not repeat")
    assert cli.analyze(p.args, source_identity=identity) == 0
    assert json.loads((output / "events.json").read_text()) == expected


def test_expired_duplicate_claims_count_and_pin_settings(env):
    _, ref = env
    ref.update({"status": "queued"})
    original = worker.claim_run("alice", RUN, "first")
    assert original["analysis_settings"] == worker.analysis_config()
    ref.update({"lease_expires_at": NOW - timedelta(minutes=1),
                "analysis_settings": {**original["analysis_settings"], "budget_usd": 3}})
    claimed = worker.claim_run("alice", RUN, "second")
    assert claimed["recovery_count"] == 1
    assert claimed["analysis_settings"]["budget_usd"] == 3
    assert worker.claim_run("alice", RUN, "duplicate") is None


def test_same_execution_retry_reclaims_live_lease_and_keeps_analysis_settings(env):
    _, ref = env
    ref.update({"status": "queued"})
    first = worker.claim_run("alice", RUN, "first", execution_id="execution-a", task_attempt=0)
    # Different executions and duplicates must never steal an active worker.
    assert worker.claim_run("alice", RUN, "other", execution_id="execution-b", task_attempt=1) is None
    assert worker.claim_run("alice", RUN, "duplicate", execution_id="execution-a", task_attempt=0) is None
    retry = worker.claim_run("alice", RUN, "retry", execution_id="execution-a", task_attempt=1)
    assert retry["worker_token"] == "retry" and retry["recovery_count"] == 1
    assert retry["analysis_settings"] == first["analysis_settings"]
    assert worker.claim_run("alice", RUN, "late", execution_id="execution-a", task_attempt=0) is None
    with pytest.raises(worker.WorkerLeaseLost):
        worker.owned_update("alice", RUN, "first", {})


def test_retry_with_missing_execution_metadata_cannot_steal_live_lease(env):
    _, ref = env
    ref.update({"status": "running", "worker_token": "legacy", "lease_expires_at": NOW+timedelta(minutes=10)})
    assert worker.claim_run("alice", RUN, "retry", execution_id="unknown", task_attempt=1) is None


def test_stale_worker_cannot_publish_events(env):
    from basketball_highlights.models import Event
    _, ref = env
    ref.update({"status": "running", "worker_token": "new"})
    event = Event(event_id="stale", timestamp_seconds=10, clip_start_seconds=5, clip_end_seconds=15,
                  classification="made_basket", needs_review=False, evidence_timestamps=[10], included=True)
    with pytest.raises(worker.WorkerLeaseLost):
        worker.save_events("alice", RUN, [(event, "private/stale.mp4", event.model_dump())], "old")
    assert not refs.event_ref("alice", RUN, "stale").get().exists


def test_empty_owned_update_checks_ownership_without_firestore_write(env):
    _, ref = env
    ref.update({"status": "running", "worker_token": "owner"})
    before = ref.get().to_dict()
    worker.owned_update("alice", RUN, "owner", {})
    assert ref.get().to_dict() == before
    with pytest.raises(worker.WorkerLeaseLost):
        worker.owned_update("alice", RUN, "stale", {})
    worker.owned_update("alice", RUN, "owner", {"event_count": 2})
    assert ref.get().to_dict()["event_count"] == 2


def test_unsent_batch_reserves_do_not_prematurely_exhaust_budget(pipeline):
    p = pipeline
    # The entire game fits actual usage, but only a small batch fits preflight costs.
    p.args.budget_usd = .003
    assert cli.analyze(p.args) == 0
    summary = json.loads((p.output / "processing_summary.json").read_text())
    assert summary["complete"]


def test_frame_failure_does_not_advance_completed_count(pipeline, monkeypatch):
    def broken(*args):
        raise ValueError("broken video frame")
    monkeypatch.setattr(cli, "sample_frame", broken)
    progress = []
    with pytest.raises(ValueError):
        cli.analyze(pipeline.args, progress_callback=progress.append)
    assert progress[-1]["completed"] == 0
    budget = json.loads((pipeline.output / "budget.json").read_text())
    assert budget["pending"] == [] and budget["request_count"] == 0


def test_retry_budget_uses_reserved_per_attempt_cost(tmp_path):
    budget = Budget(tmp_path / "budget.json", 5)
    reserve = budget.reserve(3, attempts=4)
    budget.settle(reserve, input_tokens=100, output_tokens=0, images=3,
                  request_id="successful-retry", attempts=2)
    assert budget.data["estimated_spend_usd"] == pytest.approx(.00016)


def test_terminal_worker_loss_preserves_checkpoint_events(snapshots, tmp_path, monkeypatch):
    from basketball_highlights.models import Detection
    from basketball_highlights.game_context import ANALYSIS_VERSION
    ref, objects, persistence = snapshots
    state, ledger = envelope(tmp_path)
    state["signature"]["analysis_version"] = ANALYSIS_VERSION
    state["detections"]["w000001"] = Detection(classification="made_basket", shot_type="two_pointer",
        model_confidence=.98, estimated_event_timestamp=5, evidence_timestamps=[5]).model_dump()
    persistence.publish(persistence.identity(), state, ledger)
    ref.update({"analysis_settings": worker.analysis_config(), "recovery_count": 3,
                "reserved_milli": 1000, "declared_duration_seconds": 20})
    account = refs.account_ref("alice")
    account.set({"available_milli": 0, "reserved_milli": 1000})
    assert worker.recover_run("alice", RUN, "owner")
    assert ref.get().to_dict()["status"] == "failed"
    events = list(refs.events_ref("alice", RUN).stream())
    recovered = [snap.to_dict() for snap in events if snap.id.startswith("basket_")]
    assert len(recovered) == 1 and recovered[0]["timestamp_seconds"] == 5
    assert recovered[0]["clip_key"] is None
    assert ref.get().to_dict()["analysis_checkpoint"] is not None


@pytest.mark.parametrize("code", ["credit_balance_exhausted", "organization_spend_limit_exceeded",
                                 "project_spend_limit_exceeded", "organization_usage_limit_exceeded"])
def test_billing_429_is_terminal_and_explains_required_action(code):
    exc = RuntimeError("private response body")
    exc.status_code = 429
    exc.body = {"code": code, "type": "insufficient_quota"}
    result = failure(exc)
    assert result.category == "quota_exhausted"
    assert not result.retryable
    assert result.error_code == code
    assert "before retrying" in str(result)
    assert "private response body" not in str(result)


def test_unknown_insufficient_quota_code_is_not_retried():
    exc = RuntimeError("private response body")
    exc.status_code = 429
    exc.body = {"error": {"type": "insufficient_quota", "code": "future_quota_code"}}
    result = failure(exc)
    assert not result.retryable
    assert result.category == "quota_exhausted"


def test_displayed_spend_excludes_unsent_batch_reservations(pipeline):
    p = pipeline
    # Observe progress while the first model request is still being sent.
    progress = []
    original = p.adapter.classify
    def classify(frames):
        p.adapter.gate.on_wait()
        return original(frames)
    p.adapter.classify = classify
    assert cli.analyze(p.args, progress_callback=progress.append) == 0
    assert progress[0]["spend_usd"] == 0
    assert progress[0]["remaining_usd"] < p.args.budget_usd


def test_credit_exhaustion_stops_after_one_attempt(monkeypatch):
    from basketball_highlights.decisions import DecisionsAdapter
    from basketball_highlights.video import SampledFrame
    adapter = object.__new__(DecisionsAdapter)
    adapter.retries, adapter.gate = 3, RequestGate(4)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        exc = RuntimeError("redacted")
        exc.status_code = 429
        exc.body = {"code": "credit_balance_exhausted", "type": "insufficient_quota"}
        raise exc
    adapter.client = SimpleNamespace(decisions=SimpleNamespace(create=create))
    with pytest.raises(RequestFailed, match="API credits are exhausted"):
        adapter.classify_context([SampledFrame(1, b"jpeg")], 1)
    assert len(calls) == adapter.last_attempts == 1
    with pytest.raises(RequestFailed):
        adapter.gate.acquire()


def test_worker_credit_exhaustion_refunds_all_unused_minutes(env, monkeypatch):
    from pathlib import Path
    store, ref = env
    ref.update({"status": "queued", "reserved_milli": 1000, "declared_duration_seconds": 20,
                "analysis_authorization": {"max_minutes": 1}})
    account = refs.account_ref("alice")
    account.set({"available_milli": 0, "reserved_milli": 1000})
    s3 = SimpleNamespace(download_file=lambda bucket, key, destination: Path(destination).write_bytes(b"video"))
    monkeypatch.setattr(worker.storage, "client", lambda: s3)
    monkeypatch.setattr(worker, "ffprobe", lambda path: {"duration_seconds": 20})
    monkeypatch.setattr(worker, "analysis_copy", lambda source, *args, **kwargs: (source, {}))
    monkeypatch.setattr(worker, "Checkpoints", lambda *args: SimpleNamespace(
        identity=lambda: {}, restore=lambda *args: False, publish=lambda *args: None))
    def blocked(args, **kwargs):
        (args.output_dir / "processing_summary.json").write_text(json.dumps({"complete": False, "budget": {}}))
        raise RequestFailed("quota_exhausted", False, 429, error_code="credit_balance_exhausted")
    monkeypatch.setattr(worker, "analyze", blocked)
    monkeypatch.setattr(tasks, "enqueue", lambda *args: pytest.fail("Billing failures must not retry"))
    data = worker.claim_run("alice", RUN, "owner")
    worker._run_one_claimed("alice", RUN, data, "owner")
    assert ref.get().to_dict()["status"] == "failed"
    assert "API credits are exhausted" in ref.get().to_dict()["error"]
    assert ref.get().to_dict()["processed_seconds"] == 0
    assert account.get().to_dict()["available_milli"] == 1000
    assert account.get().to_dict()["reserved_milli"] == 0


def test_unconfigured_worker_leaves_recovery_in_scheduled_outbox(env, monkeypatch):
    monkeypatch.setattr(tasks, "settings", replace(tasks.settings,
        gcp_project_id="", dispatch_url="", dispatch_service_account="", internal_audience=""))
    store, ref = env
    ref.update({"status": "running", "worker_token": "owner", "reserved_milli": 1000})
    account = refs.account_ref("alice")
    account.set({"available_milli": 0, "reserved_milli": 1000})
    monkeypatch.setattr(tasks, "enqueue", lambda *args: pytest.fail("Unconfigured workers must use scheduled dispatch"))
    assert worker.recover_run("alice", RUN, "owner")
    data = ref.get().to_dict()
    assert data["status"] == "queued" and data["recovery_count"] == 1
    assert data["worker_token"] is None
    outbox = refs.outbox_ref(RUN).get().to_dict()
    assert outbox["dispatch_attempt"] == 1 and outbox["dispatched_at"] is None
    assert outbox["run_id"] == RUN and outbox["user_id"] == "alice"
    assert account.get().to_dict()["reserved_milli"] == 1000
