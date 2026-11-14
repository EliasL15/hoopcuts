"""Media retention, migration, and cleanup fencing."""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import HTTPException

from . import storage
from .config import settings
from .db import client, utcnow

log = logging.getLogger(__name__)
RETENTION = timedelta(days=14)
ACTIVE_ANALYSIS = {"queued", "running", "cancel_requested"}
ACTIVE_REEL = {"queued", "rendering"}


def available(data: dict, now=None) -> bool:
    now = now or utcnow()
    expiry = data.get("media_expires_at")
    return not data.get("media_deleted_at") and not data.get("media_cleanup_started_at") and (
        expiry is None or expiry > now or data.get("status") in ACTIVE_ANALYSIS)


def require_media(data: dict) -> None:
    if not available(data):
        raise HTTPException(410, "Game footage and highlights have expired. Your saved reel and CSV remain available.")


def busy(data: dict, now=None) -> bool:
    now = now or utcnow()
    job = data.get("reel_job") or {}
    return (job.get("status") in ACTIVE_REEL and job.get("lease_expires_at", now) > now) or (
        data.get("media_hold_until") is not None and data["media_hold_until"] > now)


def clip_download(key: str, data: dict) -> str | None:
    if not available(data):
        return None
    expiry = data.get("media_expires_at")
    seconds = 600 if not expiry or data.get("status") in ACTIVE_ANALYSIS else min(600, int((expiry - utcnow()).total_seconds()))
    return storage.signed_download(key, expires=seconds) if seconds > 0 else None


def migrate(verify: bool = False) -> dict:
    """One fixed rollout clock; rerunning never extends a game's grace period."""
    from google.cloud import firestore
    db = client()
    marker = db.collection("maintenance").document("media-retention-v1")
    tx = db.transaction()

    @firestore.transactional
    def clock(transaction):
        data = next(transaction.get(marker)).to_dict() or {}
        if not data.get("started_at"):
            data = {"started_at": utcnow(), "verified": False}
            transaction.set(marker, data)
        return data

    rollout = clock(tx)
    migrated = missing = recovered = 0
    for snapshot in db.collection_group("runs").stream():
        data = snapshot.to_dict() or {}
        user_id = data.get("user_id")
        if not user_id:
            missing += 1
            continue
        legacy_key = storage.key(user_id, snapshot.id, "highlights_reel.mp4")
        # Inspect all legacy runs even when their deadline was assigned by a previous failed pass.
        has_legacy = not data.get("legacy_reel_checked") and not data.get("latest_reel") and storage.exists(legacy_key)
        tx = db.transaction()

        @firestore.transactional
        def update(transaction):
            current = next(transaction.get(snapshot.reference)).to_dict()
            if current is None:
                return
            changes = {}
            if not current.get("legacy_reel_checked"):
                changes["legacy_reel_checked"] = True
            if not current.get("media_expires_at"):
                changes["media_expires_at"] = rollout["started_at"] + RETENTION
                changes["retention_version"] = 1
            if has_legacy and not current.get("latest_reel"):
                changes["latest_reel"] = {"key": legacy_key, "generated_at": current.get("updated_at") or rollout["started_at"],
                                           "count": int(current.get("included_count", 0)), "format": "landscape"}
            if changes:
                transaction.update(snapshot.reference, changes)

        update(tx)
        migrated += int(not data.get("media_expires_at"))
        recovered += int(has_legacy)
    # Verification is an explicit separate pass, after recovery has succeeded for every run.
    if verify and missing == 0 and migrated == 0:
        marker.set({"verified": True, "verified_at": utcnow()}, merge=True)
    return {"migrated": migrated, "recovered_reels": recovered, "missing_owners": missing,
            "verified": bool(verify and missing == 0 and migrated == 0), "grace_expires_at": (rollout["started_at"] + RETENTION).isoformat()}



def prune_reels(ref) -> None:
    """Retry deletion of known superseded exports without touching an active candidate."""
    from google.cloud import firestore
    data = ref.get().to_dict() or {}
    keep = (data.get("latest_reel") or {}).get("key")
    removed = set()
    for key in data.get("reel_delete_pending", []):
        if key != keep:
            storage.delete_object(key)
            removed.add(key)
    if not removed:
        return
    tx = client().transaction()

    @firestore.transactional
    def acknowledge(transaction):
        current = next(transaction.get(ref)).to_dict()
        if current is not None:
            transaction.update(ref, {"reel_delete_pending": [key for key in current.get("reel_delete_pending", []) if key not in removed]})
    acknowledge(tx)

def cleanup() -> dict:
    from google.cloud import firestore
    db = client()
    marker = db.collection("maintenance").document("media-retention-v1").get().to_dict() or {}
    if not settings.media_cleanup_enabled or not marker.get("verified"):
        return {"enabled": False, "deleted": 0, "failed": 0}
    now = utcnow()
    deleted = failed = 0
    for snapshot in db.collection_group("runs").stream():
        data = snapshot.to_dict() or {}
        if data.get("reel_delete_pending"):
            try:
                prune_reels(snapshot.reference)
            except Exception:
                log.exception("Superseded reel cleanup failed for run %s", snapshot.id)
                failed += 1
        expiry = data.get("media_expires_at")
        if not expiry or expiry > now or data.get("media_deleted_at"):
            continue
        tx = db.transaction()

        @firestore.transactional
        def claim(transaction):
            current = next(transaction.get(snapshot.reference)).to_dict()
            if not current or current.get("media_deleted_at") or current.get("status") in ACTIVE_ANALYSIS or busy(current, now):
                return None
            if not current.get("media_expires_at") or current["media_expires_at"] > now:
                return None
            changes = {"media_cleanup_started_at": now}
            job = current.get("reel_job") or {}
            if job.get("status") in ACTIVE_REEL:
                from .reels import finish_quota
                finish_quota(transaction, current["user_id"], job, False)
                changes["reel_job"] = {**job, "status": "failed", "error": "Reel generation timed out. The previous reel is still available."}
            transaction.update(snapshot.reference, changes)
            return current

        current = claim(tx)
        if current is None:
            continue
        try:
            # Preserve ONLY the latest successful reel, including recovered legacy keys.
            prefix = f"users/{current['user_id']}/runs/{snapshot.id}/"
            keep = (current.get("latest_reel") or {}).get("key")
            s3 = storage.client()
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=settings.r2_bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    if obj["Key"] != keep:
                        storage.delete_object(obj["Key"])
            snapshot.reference.update({"media_deleted_at": utcnow(), "analysis_checkpoint": None, "sponsors": [], "media_cleanup_error": None})
            deleted += 1
        except Exception:
            log.exception("Media cleanup failed for run %s", snapshot.id)
            snapshot.reference.update({"media_cleanup_error": "Storage cleanup failed; retry pending"})
            failed += 1
    return {"enabled": True, "deleted": deleted, "failed": failed}
