"""Immutable, ownership-fenced analysis snapshots in private object storage."""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path

from .app import storage
from .app.config import settings
from .app.db import client, run_ref, server_timestamp, utcnow

log = logging.getLogger(__name__)
VERSION = 1


class CheckpointError(RuntimeError):
    pass


class Checkpoints:
    def __init__(self, user_id, run_id, token, config):
        self.user_id, self.run_id, self.token = user_id, run_id, token
        self.config = config
        self.ref = run_ref(user_id, run_id)
        self.prefix = storage.key(user_id, run_id, "checkpoints/")
        self.metrics = {"uploads": 0, "bytes": 0, "seconds": 0.0, "compactions": 0}

    def identity(self):
        data = self.ref.get().to_dict() or {}
        head = storage.client().head_object(Bucket=settings.r2_bucket, Key=data["source_key"])
        return {"run_id": self.run_id, "key": data["source_key"],
                "etag": head["ETag"], "bytes": head["ContentLength"]}

    def restore(self, output: Path, identity: dict) -> bool:
        data = self.ref.get().to_dict() or {}
        pointer = data.get("analysis_checkpoint")
        if not pointer:
            return False
        try:
            references = [*pointer.get("segments", []), pointer]
            if len(references) > 1024:
                raise ValueError("Unbounded checkpoint chain")
            payload = None
            for reference in references:
                if not reference["key"].startswith(self.prefix):
                    raise ValueError("Invalid checkpoint location")
                body = storage.client().get_object(Bucket=settings.r2_bucket, Key=reference["key"])["Body"].read()
                if hashlib.sha256(body).hexdigest() != reference["sha256"]:
                    raise ValueError("Checkpoint checksum mismatch")
                part = json.loads(gzip.decompress(body))
                if (part["version"] not in (VERSION, 2) or part["identity"] != identity
                        or part["config"] != self.config):
                    raise ValueError("Checkpoint source or settings mismatch")
                if part["state"].get("journal_delta"):
                    if payload is None or part["state"]["signature"] != payload["state"]["signature"]:
                        raise ValueError("Delta has no matching snapshot")
                    offset = part["budget"].get("requests_offset")
                    if offset != len(payload["budget"]["requests"]):
                        raise ValueError("Checkpoint ledger sequence mismatch")
                    for table in ("detections", "contexts"):
                        payload["state"][table].update(part["state"][table])
                    payload["state"]["errors"] = part["state"]["errors"]
                    payload["state"]["completed"] = part["state"]["completed"]
                    requests = payload["budget"]["requests"] + part["budget"]["requests"]
                    payload["budget"] = {**part["budget"], "requests": requests}
                else:
                    if payload is not None:
                        raise ValueError("Unexpected snapshot inside delta chain")
                    payload = part
            payload["state"].pop("journal_delta", None)
            payload["budget"].pop("requests_offset", None)
            state, budget = payload["state"], payload["budget"]
            if not isinstance(state["detections"], dict) or not isinstance(state["contexts"], dict):
                raise ValueError("Invalid analysis state")
            if budget["limit_usd"] != self.config["budget_usd"] or not isinstance(budget["requests"], list):
                raise ValueError("Invalid budget ledger")
            for field in ("estimated_spend_usd", "actual_spend_usd", "request_count", "input_tokens", "output_tokens", "image_count"):
                if not isinstance(budget[field], (int, float)) or budget[field] < 0:
                    raise ValueError("Invalid budget totals")
            for pending in budget["pending"]:
                if pending["reserved_usd"] < 0 or pending["attempts"] < 1:
                    raise ValueError("Invalid pending reservation")
            # Validate the entire envelope before exposing either file to analysis.
            from basketball_highlights.models import Detection, ContextResult
            for value in state["detections"].values():
                Detection.model_validate(value)
            for value in state["contexts"].values():
                ContextResult.model_validate(value)
            output.mkdir(parents=True, exist_ok=True)
            (output / "run_state.json").write_text(json.dumps(state))
            (output / "budget.json").write_text(json.dumps(budget))
            age = (utcnow() - data["checkpoint_at"]).total_seconds() if data.get("checkpoint_at") else None
            log.info("Restored analysis checkpoint: run=%s completed=%s age_seconds=%s", self.run_id, len(state["detections"]), age)
            return True
        except Exception as exc:
            raise CheckpointError("Saved analysis checkpoint is unavailable, corrupt, or incompatible") from exc

    def publish(self, identity: dict, state: dict, budget: dict):
        from google.cloud import firestore
        from .worker import WorkerLeaseLost, AnalysisCancelled
        started = time.monotonic()
        incremental = "journal_delta" in state
        delta = state.get("journal_delta", False)
        previous = (self.ref.get().to_dict() or {}).get("analysis_checkpoint") if delta else None
        if delta and previous is None:
            raise CheckpointError("A checkpoint delta requires a durable snapshot")
        body = gzip.compress(json.dumps({"version": 2 if incremental else VERSION, "identity": identity,
            "config": self.config, "state": state, "budget": budget}).encode())
        key = self.prefix + self.token + "/" + str(uuid.uuid4()) + ".json.gz"
        try:
            storage.client().put_object(Bucket=settings.r2_bucket, Key=key, Body=body,
                                        ContentType="application/gzip")
        except Exception as exc:
            raise CheckpointError("Could not save analysis progress; processing stopped safely") from exc

        @firestore.transactional
        def commit(tx):
            snapshot = next(tx.get(self.ref))
            data = snapshot.to_dict() or {}
            if not snapshot.exists or data.get("worker_token") != self.token or data.get("status") not in {"running", "cancel_requested"}:
                raise WorkerLeaseLost("Checkpoint owner was replaced")
            old = data.get("analysis_checkpoint")
            if delta and old != previous:
                raise WorkerLeaseLost("Checkpoint pointer was replaced")
            from basketball_highlights.events import make_windows
            signature = state["signature"]
            progress = {"completed": state.get("completed", sum(not key.endswith("_refine") for key in state["detections"])),
                "total": len(make_windows(signature["duration"], signature["window_seconds"], signature["stride_seconds"])),
                "spend_usd": max(budget["actual_spend_usd"], budget["estimated_spend_usd"] +
                                 sum(item["reserved_usd"] for item in budget["pending"]))}
            pointer = {"key": key, "sha256": hashlib.sha256(body).hexdigest()}
            if delta:
                pointer["segments"] = [*old.get("segments", []), {"key": old["key"], "sha256": old["sha256"]}]
            tx.update(self.ref, {"analysis_checkpoint": pointer,
                "checkpoint_at": server_timestamp(), "checkpoint_progress": progress})
            return old, data.get("status") == "cancel_requested"
        old, cancelled = commit(client().transaction())
        if old and not delta:
            for reference in [*old.get("segments", []), old]:
                try:
                    storage.delete_object(reference["key"])
                except Exception:
                    log.warning("Previous checkpoint cleanup deferred: run=%s", self.run_id)
        self.metrics["uploads"] += 1
        self.metrics["bytes"] += len(body)
        self.metrics["seconds"] += time.monotonic() - started
        self.metrics["compactions"] += int(incremental and not delta)
        if cancelled:
            raise AnalysisCancelled()
