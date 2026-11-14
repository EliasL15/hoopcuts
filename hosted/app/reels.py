"""Transactional reel snapshots and Cloud Run export worker."""
from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

from basketball_highlights.social_reel import merge_reel_windows, render_reel
from . import media, storage, tasks, billing
from .config import settings
from .db import client, events_ref, read_run, run_ref, utcnow

log = logging.getLogger(__name__)
REEL_LEASE = timedelta(minutes=110)


def queue(user_id: str, run_id: str, format: str, sponsor_ids: list[str], combine_overlaps: bool = True,
          exclude_free_throws: bool = False) -> dict:
    from google.cloud import firestore
    subscriptions = billing.enabled()
    ref = run_ref(user_id, run_id)
    job_id = str(uuid.uuid4())
    tx = client().transaction()

    @firestore.transactional
    def snapshot(transaction):
        data = next(transaction.get(ref)).to_dict()
        if data is None:
            raise HTTPException(404, "Run not found")
        media.require_media(data)
        if data.get("status") in media.ACTIVE_ANALYSIS or media.busy(data):
            raise HTTPException(409, "Wait for the current processing job to finish")
        sponsors = {item["id"]: item for item in data.get("sponsors", [])}
        if len(sponsor_ids) > 2 or len(set(sponsor_ids)) != len(sponsor_ids) or any(s not in sponsors for s in sponsor_ids):
            raise HTTPException(400, "Choose up to two sponsor logos saved on this game")
        # Read selections inside the transaction: edits during export cannot alter the snapshot.
        events = [item.to_dict() or {} for item in transaction.get(events_ref(user_id, run_id).order_by("__name__"))]
        events.sort(key=lambda item: float(item.get("timestamp_seconds", 0)))
        windows = [{"start": item["payload"]["clip_start_seconds"], "end": item["payload"]["clip_end_seconds"]}
                   for item in events if item.get("included") and not (
                       exclude_free_throws and (item.get("payload") or {}).get("shot_type") == "free_throw")]
        if not windows:
            raise HTTPException(400, "Choose at least one clip to build a reel")
        # Cache the actual exported timeline, rather than padded event clips.
        # Old exports with repeated overlap must not be returned as cache hits.
        timeline = merge_reel_windows(windows) if combine_overlaps else windows
        fingerprint = billing.reel_fingerprint(timeline, format, [sponsors[s]["key"] for s in sponsor_ids],
                                               combine_overlaps, exclude_free_throws)
        latest = data.get("latest_reel") or {}
        if latest.get("fingerprint") == fingerprint:
            return {"status": "ready", "id": latest.get("job_id"), "windows": windows,
                    "url": storage.signed_download(latest["key"]), "cached": True}
        authorization = None
        lineage = data.get("lineage_id") or run_id
        if subscriptions:
            balance = next(transaction.get(billing.account_ref(user_id))).to_dict() or {}
            sub = next(transaction.get(billing.subscription_ref(user_id))).to_dict() or {}
            authorization = billing.entitlement(balance, sub, data)
            if sponsor_ids and not authorization["sponsors"]:
                raise HTTPException(402, "Sponsor end cards require Business. Upgrade to include logos.")
            usage = next(transaction.get(billing.usage_ref(user_id, lineage))).to_dict() or {}
            if usage.get("pending"):
                raise HTTPException(409, "A reel for this game's analysis history is already processing")
            if authorization["renders"] is not None and int(usage.get("successful", 0)) >= authorization["renders"]:
                raise HTTPException(402, "This game's reel limit has been reached. Upgrade for more renders.")
            transaction.set(billing.usage_ref(user_id, lineage),
                            {**usage, "pending": job_id, "pending_run": run_id, "pending_until": utcnow() + REEL_LEASE})
        now = utcnow()
        job = {"id": job_id, "status": "queued", "format": format, "windows": windows,
               "combine_overlaps": combine_overlaps, "exclude_free_throws": exclude_free_throws,
               "sponsors": [sponsors[s]["key"] for s in sponsor_ids], "queued_at": now,
               "authorization": authorization, "lineage_id": lineage, "fingerprint": fingerprint,
               "lease_expires_at": now + timedelta(minutes=15), "error": None}
        transaction.update(ref, {"reel_job": job})
        return job

    job = snapshot(tx)
    if job.get("cached"):
        return {"id": job["id"], "status": "ready", "count": len(job["windows"]), "url": job["url"]}
    try:
        tasks.launch_reel_job(run_id, user_id, job_id)
    except Exception:
        # Scheduled dispatcher recovers queued jobs without discarding the snapshot.
        log.exception("Reel launch deferred for run %s", run_id)
    return {"id": job_id, "status": job["status"], "count": len(job["windows"]), "url": None}


def run(user_id: str, run_id: str, job_id: str) -> None:
    from google.cloud import firestore
    ref = run_ref(user_id, run_id)
    token = str(uuid.uuid4())
    tx = client().transaction()

    @firestore.transactional
    def claim(transaction):
        data = next(transaction.get(ref)).to_dict()
        if not data:
            return None
        job = data.get("reel_job") or {}
        if job.get("id") != job_id or job.get("status") != "queued":
            return None
        if not media.available(data):
            finish_quota(transaction, user_id, job, False)
            transaction.update(ref, {"reel_job": {**job, "status": "failed", "error": "Game footage has expired"}})
            return None
        if settings.subscriptions_enabled and not data.get("legacy_access") and not job.get("authorization"):
            finish_quota(transaction, user_id, job, False)
            transaction.update(ref, {"reel_job": {**job, "status": "failed", "error": "Reel authorization is missing"}})
            return None
        if job.get("sponsors") and job.get("authorization") and not job["authorization"].get("sponsors"):
            finish_quota(transaction, user_id, job, False)
            transaction.update(ref, {"reel_job": {**job, "status": "failed", "error": "Sponsor authorization is missing"}})
            return None
        job = {**job, "status": "rendering", "token": token, "lease_expires_at": utcnow() + REEL_LEASE}
        transaction.update(ref, {"reel_job": job})
        return {**data, "reel_job": job}

    data = claim(tx)
    if data is None:
        return
    work = Path(tempfile.mkdtemp(prefix="highlights-reel-"))
    reel_key = storage.key(user_id, run_id, f"reels/{job_id}.mp4")
    try:
        job = data["reel_job"]
        source = work / ("source" + Path(data["source_key"]).suffix)
        storage.client().download_file(settings.r2_bucket, data["source_key"], str(source))
        logos = []
        for index, key in enumerate(job["sponsors"]):
            path = work / f"logo-{index}.png"
            storage.client().download_file(settings.r2_bucket, key, str(path))
            logos.append(path)
        result = render_reel(source, job["windows"], work / "reel.mp4", job["format"], logos,
                             combine_overlaps=job.get("combine_overlaps", True))
        storage.client().upload_file(str(work / "reel.mp4"), settings.r2_bucket, reel_key,
                                     ExtraArgs={"ContentType": "video/mp4"})
        tx = client().transaction()

        @firestore.transactional
        def publish(transaction):
            current = next(transaction.get(ref)).to_dict() or {}
            active = current.get("reel_job") or {}
            if active.get("token") != token or active.get("status") != "rendering" or active["lease_expires_at"] <= utcnow() or current.get("media_cleanup_started_at"):
                return False, None
            finish_quota(transaction, user_id, active, True)
            previous = (current.get("latest_reel") or {}).get("key")
            transaction.update(ref, {"latest_reel": {**result, "key": reel_key, "generated_at": utcnow(),
                                                   "exclude_free_throws": job.get("exclude_free_throws", False),
                                                   "fingerprint": job.get("fingerprint"), "job_id": job_id},
                                     "reel_job": {"id": job_id, "status": "ready", "format": job["format"],
                                                  "exclude_free_throws": job.get("exclude_free_throws", False), "error": None},
                                     "reel_delete_pending": [*current.get("reel_delete_pending", []), *([previous] if previous and previous != reel_key else [])]})
            return True, previous

        accepted, previous = publish(tx)
        if not accepted:
            storage.delete_object(reel_key)
        elif previous and previous != reel_key:
            try:
                media.prune_reels(ref)
            except Exception:
                log.exception("Old reel deletion deferred for run %s", run_id)
    except Exception:
        log.exception("Reel generation failed for run %s", run_id)
        tx = client().transaction()

        @firestore.transactional
        def fail(transaction):
            current = next(transaction.get(ref)).to_dict() or {}
            job = current.get("reel_job") or {}
            if job.get("token") == token and job.get("status") == "rendering":
                finish_quota(transaction, user_id, job, False)
                transaction.update(ref, {"reel_job": {"id": job_id, "status": "failed", "error": "Reel generation failed. Try again; your previous reel is still available."}})
        fail(tx)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def recover() -> dict:
    from google.cloud import firestore
    launched = timed_out = 0
    now = utcnow()
    for item in client().collection_group("runs").stream():
        data = item.to_dict() or {}
        job = data.get("reel_job") or {}
        if job.get("status") not in media.ACTIVE_REEL or job.get("lease_expires_at", now) > now:
            continue
        tx = client().transaction()

        @firestore.transactional
        def retry(transaction):
            current = next(transaction.get(item.reference)).to_dict() or {}
            active = current.get("reel_job") or {}
            if active.get("id") != job["id"] or active.get("status") not in media.ACTIVE_REEL or active.get("lease_expires_at", now) > now:
                return None
            if active["status"] == "rendering" or not media.available(current):
                finish_quota(transaction, current["user_id"], active, False)
                transaction.update(item.reference, {"reel_job": {"id": active["id"], "status": "failed", "error": "Reel generation timed out or footage expired. Your previous reel remains available."}})
                return "failed"
            transaction.update(item.reference, {"reel_job": {**active, "lease_expires_at": now + timedelta(minutes=15)}})
            return "queued"

        status = retry(tx)
        if status == "queued":
            try:
                tasks.launch_reel_job(item.id, data["user_id"], job["id"])
                launched += 1
            except Exception:
                log.exception("Reel redispatch failed for run %s", item.id)
        elif status == "failed":
            timed_out += 1
    return {"launched": launched, "timed_out": timed_out}


def finish_quota(transaction, uid, job, success):
    if not job.get("authorization"):
        return
    ref = billing.usage_ref(uid, job["lineage_id"])
    usage = next(transaction.get(ref)).to_dict() or {}
    if usage.get("pending") == job.get("id"):
        transaction.set(ref, {**usage, "successful": int(usage.get("successful", 0)) + int(success),
                              "pending": None, "pending_run": None, "pending_until": None})
