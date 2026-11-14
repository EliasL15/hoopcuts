"""An explicitly published sample, separate from private runs and their retention."""
from __future__ import annotations

import uuid
from fastapi import HTTPException
from . import storage, media
from .config import settings
from .db import client, read_run, read_events, utcnow

EVENT_FIELDS = {"event_id", "timestamp_seconds", "classification", "shot_type",
                "model_confidence", "needs_review", "game_context"}


def demo_ref():
    return client().collection("product_demos").document("basketball")


def load() -> dict | None:
    snapshot = demo_ref().get()
    return snapshot.to_dict() if snapshot.exists else None


def summary(data: dict | None) -> dict:
    if not data:
        return {"available": False}
    return {"available": True, "name": data["name"], "event_count": len(data["events"])}


def detail(data: dict | None) -> dict:
    if not data:
        raise HTTPException(404, "The sample game is not available yet.")
    prefix = f"demos/{data['id']}/"
    def sign(key):
        if not key or not key.startswith(prefix):
            raise HTTPException(503, "The sample game is temporarily unavailable.")
        return storage.signed_download(key, expires=3600)
    events = [{**{k: v for k, v in event.items() if k in EVENT_FIELDS or k == "included"},
               "clip_url": sign(event["clip_key"])} for event in data["events"]]
    return {"id": data["id"], "name": data["name"], "status": "complete",
            "event_count": len(events), "included_count": sum(e["included"] for e in events),
            "media_available": True, "events": events, "is_admin": False,
            "latest_reel": {"count": data["reel_count"], "format": data["reel_format"]},
            "example_reel_url": sign(data["reel_key"])}


def publish(user_id: str, run_id: str) -> dict:
    """Copy only demo clips/reel; publish atomically after all copies succeed.

    Called by the operator CLI, never by an account's run mutation endpoints.
    Existing demo versions are kept for active viewers and operator rollback.
    """
    run = read_run(user_id, run_id)
    if not run or run.get("status") != "complete":
        raise ValueError("The sample must be a completed game.")
    media.require_media(run)
    reel = run.get("latest_reel") or {}
    if not reel.get("key"):
        raise ValueError("Build an example reel before publishing the sample.")
    source_events = read_events(user_id, run_id)
    if not source_events or any(not e.get("clip_key") for e in source_events):
        raise ValueError("Every sample moment must have a playable clip.")
    source_prefix = storage.key(user_id, run_id, "")
    keys = [e["clip_key"] for e in source_events] + [reel["key"]]
    if any(not key.startswith(source_prefix) for key in keys):
        raise ValueError("Sample media must belong to the source game.")
    if not all(storage.exists(key) for key in keys):
        raise ValueError("Some sample media is missing. The existing demo was preserved.")
    demo_id = str(uuid.uuid4())
    prefix = f"demos/{demo_id}/"
    s3 = storage.client()
    def copy(source, destination):
        s3.copy_object(Bucket=settings.r2_bucket, Key=destination,
                       CopySource={"Bucket": settings.r2_bucket, "Key": source})
        return destination
    events = []
    for index, event in enumerate(source_events):
        payload = event.get("payload") or {}
        events.append({**{k: v for k, v in payload.items() if k in EVENT_FIELDS},
                       "event_id": payload.get("event_id") or event["id"],
                       "included": bool(event.get("included")),
                       "clip_key": copy(event["clip_key"], f"{prefix}clip-{index}.mp4")})
    data = {"id": demo_id, "name": "Sample basketball game", "events": events,
            "reel_key": copy(reel["key"], f"{prefix}example-reel.mp4"),
            "reel_count": int(reel.get("count", 0)), "reel_format": reel.get("format", "vertical"),
            "published_at": utcnow()}
    demo_ref().set(data)
    return summary(data)
