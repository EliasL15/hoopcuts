"""Cloud Run Job entrypoint. One invocation handles exactly one run."""
from __future__ import annotations

import json
import logging
import threading
import os
import shutil
import tempfile
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal, ROUND_UP
from pathlib import Path

from basketball_highlights.cli import analyze
from basketball_highlights.retry import RequestFailed
from .checkpoints import Checkpoints, CheckpointError
from basketball_highlights.social_reel import analysis_copy
from basketball_highlights.models import Event
from basketball_highlights.performance import Performance
from basketball_highlights.video import ffprobe

from .app import storage, media
from .app.config import settings, analysis_settings
from .app import billing
from .app.db import (account_ref, client, event_ref, events_ref, ledger_ref,
                    read_events, read_run, run_ref, server_timestamp, utcnow)


MAX_RECOVERIES = 3


def analysis_config():
    return analysis_settings(settings)


class AnalysisCancelled(Exception):
    pass


class WorkerLeaseLost(Exception):
    pass


def reported_spend(budget: dict) -> Decimal:
    return max(Decimal(str(budget.get("actual_spend_usd", 0))),
               Decimal(str(budget.get("estimated_spend_usd", 0))) +
               sum((Decimal(str(item["reserved_usd"])) for item in budget.get("pending", [])), Decimal(0)))


def milli(value) -> int:
    return int((Decimal(str(value)) * 1000).quantize(Decimal("1"), rounding=ROUND_UP))


def minutes(milli_value) -> float:
    return float(Decimal(int(milli_value or 0)) / Decimal(1000))


def claim_run(user_id: str, run_id: str, worker_token: str, *, execution_id=None, task_attempt=0) -> dict | None:
    from google.cloud import firestore

    ref = run_ref(user_id, run_id)
    transaction = client().transaction()
    now = utcnow()

    @firestore.transactional
    def claim(tx):
        snapshot = next(tx.get(ref))
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        status = data.get("status")
        if status in {"cancelled", "complete", "failed", "cancel_requested"}:
            return None
        lease = data.get("lease_expires_at")
        same_execution_retry = (execution_id is not None and data.get("worker_execution") == execution_id
                                and task_attempt > int(data.get("worker_task_attempt", 0)))
        if status == "running" and lease and lease > now and not same_execution_retry:
            return None
        if status not in {"queued", "running"}:
            return None
        recovery_count = int(data.get("recovery_count", 0))
        if status == "running":
            if recovery_count >= MAX_RECOVERIES:
                return None
            recovery_count += 1
        config = data.get("analysis_settings") or analysis_config()
        data["analysis_settings"] = config
        data["recovery_count"] = recovery_count
        data.update({"status": "running", "worker_token": worker_token,
                     "lease_expires_at": now + timedelta(minutes=10), "updated_at": now,
                     "worker_execution": execution_id, "worker_task_attempt": task_attempt})
        tx.update(ref, {"recovery_count": recovery_count, "analysis_settings": config, "status": "running", "worker_token": worker_token,
                        "worker_execution": execution_id, "worker_task_attempt": task_attempt,
                        "lease_expires_at": now + timedelta(minutes=10),
                        "updated_at": server_timestamp(), "error": None})
        data["error"] = None
        return data

    return claim(transaction)


def update_progress(user_id: str, run_id: str, worker_token: str, value: dict) -> None:
    from google.cloud import firestore

    ref = run_ref(user_id, run_id)
    transaction = client().transaction()

    @firestore.transactional
    def update(tx):
        snapshot = next(tx.get(ref))
        if not snapshot.exists:
            raise WorkerLeaseLost("Run was deleted")
        data = snapshot.to_dict() or {}
        if data.get("worker_token") != worker_token:
            raise WorkerLeaseLost("Worker lease was superseded")
        if data.get("status") == "cancel_requested":
            raise AnalysisCancelled()
        tx.update(ref, {"status": "running", "progress": value,
                        "updated_at": server_timestamp(),
                        "lease_expires_at": utcnow() + timedelta(minutes=10)})

    update(transaction)


def renew_lease(user_id: str, run_id: str, worker_token: str) -> bool:
    """Renew ownership without replacing progress or undoing cancellation."""
    from google.cloud import firestore
    ref = run_ref(user_id, run_id)

    @firestore.transactional
    def renew(tx):
        snapshot = next(tx.get(ref))
        data = snapshot.to_dict() or {}
        if (not snapshot.exists or data.get("worker_token") != worker_token
                or data.get("status") not in {"running", "cancel_requested"}):
            return False
        tx.update(ref, {"lease_expires_at": utcnow() + timedelta(minutes=10),
                        "updated_at": server_timestamp()})
        return True

    return renew(client().transaction())


def maintain_lease(stop: threading.Event, user_id: str, run_id: str, worker_token: str) -> None:
    while not stop.wait(60):
        try:
            if not renew_lease(user_id, run_id, worker_token):
                return
        except Exception:
            # Retry temporary database failures; progress and settlement also
            # check ownership transactionally.
            logging.exception("Could not renew analysis worker lease")


def settle(user_id: str, run_id: str, worker_token: str, reserved_milli: int,
           duration: Decimal, completed_ratio: float, finish_status: str,
           error: str | None, spend: Decimal = Decimal(0)) -> None:
    from google.cloud import firestore

    actual_seconds = max(Decimal(0), duration * Decimal(str(max(0, min(1, completed_ratio)))))
    actual_milli = milli(actual_seconds / Decimal(60))
    actual_milli = min(actual_milli, reserved_milli)
    key = f"run:{run_id}:settle"
    ref = run_ref(user_id, run_id)
    account = account_ref(user_id)
    receipt = ledger_ref(user_id, key)
    transaction = client().transaction()

    @firestore.transactional
    def apply(tx):
        snapshot = next(tx.get(ref))
        if not snapshot.exists:
            return
        data = snapshot.to_dict() or {}
        if data.get("worker_token") != worker_token:
            return
        final_status = "cancelled" if data.get("status") == "cancel_requested" else finish_status
        if not next(tx.get(receipt)).exists:
            balance = next(tx.get(account)).to_dict() or {"available_milli": 0, "reserved_milli": 0}
            released = billing.release(balance, {**data, "reserved_milli": reserved_milli}, actual_milli, utcnow())
            refund = sum(int(released.get(bucket, 0)) - int(balance.get(bucket, 0)) for bucket in billing.BALANCES)
            tx.set(account, {**released, "updated_at": server_timestamp()}, merge=True)
            tx.set(receipt, {"event_key": key, "run_id": run_id, "kind": "settle",
                "available_delta_milli": refund, "reserved_delta_milli": -reserved_milli,
                "description": f"Settled {minutes(actual_milli):g} video minutes",
                "created_at": server_timestamp()})
        tx.update(ref, {"status": final_status, "processed_seconds": float(actual_seconds),
                        "error": None if final_status == "cancelled" else error, "spend_usd": float(spend), "updated_at": server_timestamp(),
                        "worker_token": None, "lease_expires_at": None,
                        "completed_at": server_timestamp(),
                        "media_expires_at": (utcnow() + media.RETENTION if final_status != "failed" else
                                             data.get("created_at", utcnow()) + media.RETENTION)})

    apply(transaction)
    latest = ref.get().to_dict() or {}
    if latest.get("status") in {"complete", "cancelled"} and latest.get("analysis_checkpoint"):
        try:
            storage.delete_prefix(storage.key(user_id, run_id, "checkpoints/"))
            ref.update({"analysis_checkpoint": None})
        except Exception:
            logging.warning("Terminal checkpoint cleanup deferred: run=%s", run_id)


def owned_update(user_id: str, run_id: str, token: str, values: dict) -> None:
    from google.cloud import firestore
    ref = run_ref(user_id, run_id)

    @firestore.transactional
    def write(tx):
        data = next(tx.get(ref)).to_dict() or {}
        if data.get("worker_token") != token or data.get("status") not in {"running", "cancel_requested"}:
            raise WorkerLeaseLost("Result owner was replaced")
        # An empty payload is an ownership check before uploading, not a write.
        # Firestore rejects update({}) even when our in-memory test double did not.
        if values:
            tx.update(ref, values)
    write(client().transaction())


def recorded_progress(data):
    live = data.get("progress") or {}
    saved = data.get("checkpoint_progress") or {}
    best = max((live, saved), key=lambda value: value.get("completed", 0))
    return {**best, "spend_usd": max(live.get("spend_usd", 0), saved.get("spend_usd", 0))}


def recover_run(user_id: str, run_id: str, expected_token: str | None, *, expired=False,
                reason="Temporary model API failure") -> bool:
    """Queue bounded recovery without releasing the original reservation."""
    from google.cloud import firestore
    from .app.db import outbox_ref
    ref = run_ref(user_id, run_id)
    box = outbox_ref(run_id)
    terminal_token = str(uuid.uuid4())

    @firestore.transactional
    def recover(tx):
        data = next(tx.get(ref)).to_dict() or {}
        outbox = next(tx.get(box)).to_dict() or {}
        if data.get("worker_token") != expected_token or data.get("status") not in {"running", "cancel_requested"}:
            return None
        if expired and data.get("lease_expires_at") and data["lease_expires_at"] > utcnow():
            return None
        count = int(data.get("recovery_count", 0))
        terminal = data.get("status") == "cancel_requested" or count >= MAX_RECOVERIES
        if terminal:
            tx.update(ref, {"worker_token": terminal_token})
            return data, None
        attempt = int(outbox.get("dispatch_attempt", 0)) + 1
        tx.update(ref, {"status": "queued", "worker_token": None, "lease_expires_at": None,
            "recovery_count": count + 1, "error": reason + "; resuming saved progress",
            "updated_at": server_timestamp()})
        tx.set(box, {"run_id": run_id, "user_id": user_id, "dispatch_attempt": attempt,
                     "dispatched_at": None}, merge=True)
        return data, attempt

    result = recover(client().transaction())
    if result is None:
        return False
    data, attempt = result
    logging.info("Analysis recovery: run=%s count=%s reason=%s", run_id, data.get("recovery_count", 0), reason)
    if attempt is None:
        if data.get("analysis_checkpoint"):
            try:
                publish_partial_checkpoint(user_id, run_id, terminal_token, data)
            except Exception:
                logging.warning("Could not recover partial analysis results: run=%s", run_id)
        progress = recorded_progress(data)
        ratio = float(progress.get("completed", 0)) / max(1, float(progress.get("total", 1)))
        settle(user_id, run_id, terminal_token, int(data.get("reserved_milli", 0)),
               Decimal(str(data.get("declared_duration_seconds", 0))), ratio, "failed",
               "Analysis could not finish after three recoveries: " + reason,
               Decimal(str(progress.get("spend_usd", 0))))
    else:
        from .app import tasks
        if all((tasks.settings.gcp_project_id, tasks.settings.dispatch_url,
                tasks.settings.dispatch_service_account, tasks.settings.internal_audience)):
            try:
                tasks.enqueue(run_id, attempt)
            except Exception:
                logging.exception("Recovery dispatch deferred to scheduled outbox recovery")
        else:
            logging.info("Recovery queued for scheduled dispatcher: run=%s attempt=%s", run_id, attempt)
    return True


def save_events(user_id: str, run_id: str, saved: list[tuple[Event, str | None, dict]], worker_token: str | None = None) -> None:
    from google.cloud import firestore

    db = client()
    ref = run_ref(user_id, run_id)
    included_count = 0
    for start in range(0, len(saved), 400):
        group = saved[start:start + 400]
        tx = db.transaction()

        @firestore.transactional
        def publish(transaction):
            current = next(transaction.get(ref)).to_dict() or {}
            if worker_token is not None and (current.get("worker_token") != worker_token or
                    current.get("status") not in {"running", "cancel_requested"}):
                raise WorkerLeaseLost("Event owner was replaced")
            rows = []
            included = 0
            for event, clip_key, payload in group:
                event_document = event_ref(user_id, run_id, event.event_id)
                existing = next(transaction.get(event_document)).to_dict() or {}
                selected = bool(existing.get("included", event.included))
                included += int(selected)
                rows.append((event_document, {"event_id": event.event_id,
                    "timestamp_seconds": float(event.timestamp_seconds), "classification": event.classification,
                    "shot_type": event.shot_type, "model_confidence": event.model_confidence,
                    "needs_review": bool(event.needs_review), "game_context": event.game_context,
                    "included": selected, "payload": payload, "clip_key": clip_key,
                    "updated_at": server_timestamp()}))
            for document, row in rows:
                transaction.set(document, row)
            return included
        included_count += publish(tx)
    values = {"event_count": len(saved), "included_count": included_count, "updated_at": server_timestamp()}
    if worker_token is not None:
        owned_update(user_id, run_id, worker_token, values)
    else:
        ref.set(values, merge=True)


def publish_partial_checkpoint(user_id, run_id, token, data):
    """Recover event data on terminal worker loss without further model calls."""
    from basketball_highlights.cli import write_results, effective_detections
    from basketball_highlights.budget import Budget
    from basketball_highlights.models import Detection, ContextResult
    from basketball_highlights.game_context import ANALYSIS_VERSION
    with tempfile.TemporaryDirectory(prefix="highlights-recovery-") as directory:
        output = Path(directory)
        config = data.get("analysis_settings") or analysis_config()
        checkpoints = Checkpoints(user_id, run_id, token, config)
        if not checkpoints.restore(output, checkpoints.identity()):
            return
        state = json.loads((output / "run_state.json").read_text())
        if state["signature"].get("analysis_version") != ANALYSIS_VERSION:
            raise CheckpointError("Saved analysis version differs")
        detections = {key: Detection.model_validate(value) for key, value in state["detections"].items()}
        contexts = {key: ContextResult.model_validate(value) for key, value in state["contexts"].items()}
        duration = state["signature"]["duration"]
        budget = Budget(output / "budget.json", config["budget_usd"])
        events = write_results(output, {"duration_seconds": duration}, budget,
            effective_detections(detections), state["errors"], time.monotonic(), duration,
            config["pre_padding"], config["post_padding"], output / "unused.mp4",
            create_media=False, complete=False, create_reel_output=False, context_results=contexts)
        save_events(user_id, run_id, [(event, None, event.model_dump()) for event in events], token)


def _run_one_claimed(user_id: str, run_id: str, data: dict, worker_token: str) -> None:
    source_key = data["source_key"]
    reserved = int(data.get("reserved_milli", 0))
    progress = recorded_progress(data)
    work = Path(tempfile.mkdtemp(prefix=f"highlights-{run_id}-"))
    duration = Decimal(str(data.get("declared_duration_seconds", 0)))
    performance = Performance()
    metrics = {}
    checkpoint_manager = None
    preview_uploads = None
    last_report = float("-inf")

    def compute_cost(elapsed):
        prior = ((data.get("media_metrics") or {}).get("performance") or {})
        return prior.get("estimated_compute_usd", 0) + elapsed * (
            settings.worker_cpu * settings.worker_cpu_usd_per_second +
            settings.worker_memory_gib * settings.worker_memory_usd_per_second)

    def report_progress(value):
        nonlocal progress, last_report
        old_phase = progress.get("phase")
        progress = {**progress, **value}
        if value.get("phase") == "complete":
            progress["phase"] = ("uploading_clips" if preview_uploads and preview_uploads.pending else "saving_results")
        if progress.get("phase") == "uploading_clips":
            progress.update(stage_completed=progress.get("uploads_completed", 0), stage_total=progress.get("uploads_total", 0))
        elif progress.get("phase") in {"downloading_video", "restoring_progress", "preparing_video", "saving_results"}:
            progress.pop("stage_completed", None)
            progress.pop("stage_total", None)
        if progress.get("phase") not in {"analyzing", "refining_candidates", "checking_game_context"}:
            progress.pop("active_requests", None)
        now = time.monotonic()
        if old_phase == progress.get("phase") and now-last_report < 5:
            return
        update_progress(user_id, run_id, worker_token, {**progress,
            "estimated_compute_usd": compute_cost(time.monotonic() - performance.started)})
        last_report = now

    def performance_report():
        report = performance.report()
        summary_path = work / "result" / "performance.json"
        if summary_path.exists():
            analysis_report = json.loads(summary_path.read_text())
            report["analysis"] = analysis_report
            report["stage_seconds"].update(analysis_report["stage_seconds"])
        report["checkpoints"] = getattr(checkpoint_manager, "metrics", {})
        prior = ((data.get("media_metrics") or {}).get("performance") or {})
        elapsed = prior.get("cumulative_worker_seconds", 0) + report["elapsed_seconds"]
        report.update(cumulative_worker_seconds=elapsed,
            estimated_compute_usd=compute_cost(report["elapsed_seconds"]),
            allocated_cpu=settings.worker_cpu, allocated_memory_gib=settings.worker_memory_gib,
            compute_estimate_excludes_free_tier_and_network=True)
        if data.get("analysis_queued_at"):
            report["end_to_end_seconds"] = max(0, (utcnow() - data["analysis_queued_at"]).total_seconds())
        return report

    def check_owner():
        latest = run_ref(user_id, run_id).get().to_dict() or {}
        if latest.get("worker_token") != worker_token or latest.get("status") not in {"running", "cancel_requested"}:
            raise WorkerLeaseLost("Analysis owner was replaced")
        if latest.get("status") == "cancel_requested":
            raise AnalysisCancelled()

    try:
        source = work / Path(source_key).name
        report_progress({"phase": "downloading_video"})
        with performance.measure("download"):
            storage.client().download_file(settings.r2_bucket, source_key, str(source))
        performance.sample_memory()
        info = ffprobe(source)
        duration = Decimal(str(info["duration_seconds"]))
        if duration / Decimal(60) > Decimal(reserved) / Decimal(1000):
            raise ValueError("Uploaded video's actual duration exceeds the reserved credits")
        authorization = data.get("analysis_authorization")
        if settings.subscriptions_enabled and not data.get("legacy_access") and not authorization:
            raise ValueError("Analysis authorization is missing")
        if authorization and duration / Decimal(60) > Decimal(str(authorization["max_minutes"])):
            raise ValueError("Video exceeds its authorized plan duration")
        output = work / "result"
        output.mkdir()
        config = data["analysis_settings"]
        checkpoints = Checkpoints(user_id, run_id, worker_token, config)
        checkpoint_manager = checkpoints
        identity = checkpoints.identity()
        if data.get("analysis_checkpoint"):
            report_progress({"phase": "restoring_progress"})
        restored = checkpoints.restore(output, identity)
        report_progress({**progress, "phase": "preparing_video", "completed": progress.get("completed", 0), "total": progress.get("total", 1)})
        direct = config.get("pipeline_version", 1) == 2 and config.get("direct_source", False)
        with performance.measure("preparation"):
            if direct:
                analysis_source = source
                metrics = {"transcoded": False, "original_bytes": source.stat().st_size,
                           "analysis_bytes": source.stat().st_size, "transcode_seconds": 0.0}
            else:
                analysis_source, metrics = analysis_copy(source, work / "analysis.mp4", on_tick=lambda: report_progress({**progress, "phase": "preparing_video", "completed": progress.get("completed", 0), "total": progress.get("total", 1)}))
        allowance = None
        if config.get("pipeline_version", 1) == 2 and (config.get("rate_rpm", 0) or config.get("rate_tpm", 0)):
            from .allowance import HostedAllowance
            allowance = HostedAllowance(config.get("rate_rpm", 0), config.get("rate_tpm", 0), check_owner)
        def upload_event(event, supplied_path=None, remove_local=False):
            clip_key = None
            byte_count = 0
            if event.clip_path:
                clip_path = supplied_path or (output / event.clip_path).resolve()
                if clip_path.is_file():
                    byte_count = clip_path.stat().st_size
                    # Invocation-specific objects prevent stale uploads overwriting a new worker's clips.
                    owned_update(user_id, run_id, worker_token, {})
                    clip_key = storage.key(user_id, run_id, f"clips/{worker_token}/{event.event_id}.mp4")
                    with performance.measure("uploads"):
                        storage.client().upload_file(str(clip_path), settings.r2_bucket, clip_key,
                                                     ExtraArgs={"ContentType": "video/mp4"})
                    owned_update(user_id, run_id, worker_token, {})
                    if remove_local:
                        clip_path.unlink(missing_ok=True)
                    performance.sample_memory()
            payload = event.model_dump()
            payload["clip_path"] = None
            return (event, clip_key, payload), byte_count
        if config.get("overlap_preview_uploads", False):
            from .preview_uploads import PreviewUploads
            preview_uploads = PreviewUploads(upload_event, performance, on_wait=check_owner,
                on_progress=lambda completed: report_progress({"uploads_completed": completed,
                    "uploads_total": progress.get("clips_completed", completed)}))
        analysis_started = time.monotonic()
        args = type("Args", (), {**config, "video": analysis_source, "output_dir": output,
            "dry_run": False, "resume": restored, "csv_only": False, "limit_seconds": None,
            "verbose": False, "create_reel_output": False, "preview_max_dimension": 720 if direct else None,
            "media_ready": preview_uploads.submit if preview_uploads else None})()
        try:
            kwargs = {"progress_callback": report_progress,
                      "checkpoint_callback": lambda state, budget: checkpoints.publish(identity, state, budget),
                      "source_identity": identity}
            if allowance:
                kwargs["request_allowance"] = allowance
            try:
                result = analyze(args, **kwargs)
            finally:
                if preview_uploads:
                    preview_uploads.finish()
                    report_progress({"phase": "saving_results"})
            if result != 0:
                raise RuntimeError(f"Analysis exited with status {result}")
            status_final = "complete"; error = None; ratio = 1.0
        except (RequestFailed, CheckpointError) as exc:
            if isinstance(exc, RequestFailed) and exc.retryable and int(data.get("recovery_count", 0)) < MAX_RECOVERIES:
                owned_update(user_id, run_id, worker_token, {"media_metrics": {**metrics, "performance": performance_report()}})
                if recover_run(user_id, run_id, worker_token, reason=exc.category):
                    return
                raise WorkerLeaseLost("Recovery owner was replaced") from exc
            status_final = "failed"
            error = ("Analysis could not finish after three recoveries: " + exc.category
                     if isinstance(exc, RequestFailed) and exc.retryable else str(exc))
            latest = read_run(user_id, run_id) or {}
            last = latest.get("progress") or progress
            ratio = float(last.get("completed", 0)) / max(1, float(last.get("total", 1)))
        except Exception as exc:
            if not isinstance(exc, AnalysisCancelled):
                from basketball_highlights.budget import BudgetExceeded
                if not isinstance(exc, BudgetExceeded):
                    raise
                status_final = "failed"; error = "Model budget exhausted before analysis finished"
            else:
                status_final = "cancelled"; error = None
            latest = read_run(user_id, run_id) or {}
            last = latest.get("progress") or progress
            ratio = float(last.get("completed", 0)) / max(1, float(last.get("total", 1)))

        events_path = output / "events.json"
        events = [Event.model_validate(item) for item in json.loads(events_path.read_text())] if events_path.exists() else []
        summary_path = output / "processing_summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        spend = reported_spend(summary.get("budget", {}))
        if status_final == "complete" and not summary.get("complete", False):
            status_final = "failed"; error = "Analysis stopped with unfinished video windows"
        if preview_uploads:
            uploaded = [preview_uploads.results.get(event.event_id) or upload_event(event) for event in events]
        elif config.get("pipeline_version", 1) == 2:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="preview-upload") as executor:
                report_progress({"phase": "uploading_clips", "uploads_completed": 0, "uploads_total": len(events)})
                pending = {executor.submit(upload_event, event): event for event in events}
                uploaded_by_id = {}
                from concurrent.futures import FIRST_COMPLETED, wait
                while pending:
                    finished, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                    for future in finished:
                        event = pending.pop(future)
                        uploaded_by_id[event.event_id] = future.result()
                    report_progress({"uploads_completed": len(uploaded_by_id), "uploads_total": len(events)})
                uploaded = [uploaded_by_id[event.event_id] for event in events]
        else:
            uploaded = [upload_event(event) for event in events]
        saved = [value[0] for value in uploaded]
        report_progress({"phase": "saving_results"})
        preview_bytes = sum(value[1] for value in uploaded)
        owned_update(user_id, run_id, worker_token, {"media_metrics": {**metrics, "preview_bytes": preview_bytes,
            "game_context": summary.get("game_context"),
            "performance": performance_report(),
            "analysis_seconds": time.monotonic() - analysis_started}})
        save_events(user_id, run_id, saved, worker_token)
        latest = read_run(user_id, run_id) or {}
        last = latest.get("progress") or {}
        if status_final == "complete":
            ratio = 1.0
        settle(user_id, run_id, worker_token, reserved, duration, ratio, status_final, error, spend)
    except WorkerLeaseLost:
        return
    except Exception as exc:
        latest = read_run(user_id, run_id) or {}
        last = latest.get("progress") or progress
        ratio = float(last.get("completed", 0)) / max(1, float(last.get("total", 1)))
        spend = Decimal(0)
        summary_path = work / "result" / "processing_summary.json"
        ledger_path = work / "result" / "budget.json"
        if ledger_path.exists():
            spend = reported_spend(json.loads(ledger_path.read_text()))
        if summary_path.exists():
            try:
                spend = reported_spend(json.loads(summary_path.read_text()).get("budget", {}))
            except Exception:
                pass
        owned_update(user_id, run_id, worker_token, {"media_metrics": {**metrics, "performance": performance_report()}})
        settle(user_id, run_id, worker_token, reserved, duration, ratio, "failed",
               None if isinstance(exc, AnalysisCancelled) else f"{type(exc).__name__}: {exc}", spend)
        raise
    finally:
        if preview_uploads:
            preview_uploads.finish()
        shutil.rmtree(work, ignore_errors=True)


def run_one(run_id: str, user_id: str) -> None:
    try:
        run_id = str(uuid.UUID(run_id))
    except ValueError as exc:
        raise ValueError("RUN_ID must be a UUID") from exc
    worker_token = str(uuid.uuid4())
    logging.info("Analysis invocation: run=%s execution=%s", run_id, os.getenv("CLOUD_RUN_EXECUTION", "local"))
    data = claim_run(user_id, run_id, worker_token,
                     execution_id=os.getenv("CLOUD_RUN_EXECUTION"),
                     task_attempt=int(os.getenv("CLOUD_RUN_TASK_ATTEMPT", "0")))
    if data is None:
        return
    stop = threading.Event()
    heartbeat = threading.Thread(target=maintain_lease,
        args=(stop, user_id, run_id, worker_token), daemon=True)
    heartbeat.start()
    try:
        _run_one_claimed(user_id, run_id, data, worker_token)
    finally:
        stop.set()
        heartbeat.join(timeout=5)



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_id = os.getenv("RUN_ID")
    user_id = os.getenv("USER_ID")
    if not run_id or not user_id:
        raise SystemExit("RUN_ID and USER_ID are required")
    reel_id = os.getenv("REEL_ID")
    if reel_id:
        from .app.reels import run as run_reel
        run_reel(user_id, str(uuid.UUID(run_id)), str(uuid.UUID(reel_id)))
    else:
        run_one(run_id, user_id)
