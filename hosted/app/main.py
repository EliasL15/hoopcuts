from __future__ import annotations

import csv
import io
import json
import math
import re
import uuid
import zlib

import stripe
from datetime import timedelta
from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, File, UploadFile
from fastapi.responses import FileResponse, Response, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import storage, tasks, media, reels, billing, demo, seo
from .auth import current_user, is_admin
from .config import settings, analysis_settings
from .db import (account_ref, client, event_ref, events_ref, isoformat, ledger_ref,
                 outbox_ref, read_events, read_run, run_ref, runs_ref, server_timestamp,
                 utcnow)

app = FastAPI(title="Basketball Highlights Hosted", docs_url=None, redoc_url=None)
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "dist"
app.mount("/assets", StaticFiles(directory=FRONTEND / "assets", check_dir=False), name="frontend-assets")
SUPPORTED = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}
MILLI = Decimal(1000)


@app.middleware("http")
async def migration_maintenance(request: Request, call_next):
    if settings.billing_migration_mode and request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        return JSONResponse({"detail": "Billing maintenance is in progress. Try again shortly."}, status_code=503)
    return await call_next(request)


# Safe API errors; do not expose Stripe responses or credential-bearing logs.
@app.exception_handler(stripe.StripeError)
async def stripe_error(request: Request, error: Exception):
    return JSONResponse({"detail": "Stripe billing is temporarily unavailable. Try again shortly."}, status_code=503)


class UploadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    file_name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)


class ReelRequest(BaseModel):
    format: Literal["vertical", "landscape"] = "vertical"
    sponsor_ids: list[str] = Field(default_factory=list, max_length=2)
    combine_overlaps: bool = True
    exclude_free_throws: bool = True


class MigrationRequest(BaseModel):
    verify: bool = False


class LiveTransitionRequest(BaseModel):
    verify: bool = False
    dry_run: bool = True


class Selection(BaseModel):
    included: bool


class SubscriptionRequest(BaseModel):
    plan: Literal["pro", "business"]
    interval: Literal["monthly", "annual"]
    quote_id: str | None = Field(default=None, min_length=1, max_length=100)


class CheckoutRequest(BaseModel):
    pack_key: str = "60"


def milli(value) -> int:
    return int((Decimal(str(value)) * MILLI).quantize(Decimal("1"), rounding=ROUND_UP))


def minutes_from_milli(value) -> float:
    return float(Decimal(int(value or 0)) / MILLI)


def owned_run(user_id: str, run_id: str) -> tuple[object, dict]:
    try:
        uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(404, "Run not found") from exc
    ref = run_ref(user_id, run_id)
    snapshot = ref.get()
    if not snapshot.exists:
        raise HTTPException(404, "Run not found")
    return ref, {"id": snapshot.id, **(snapshot.to_dict() or {})}


def run_summary(row: dict, *, admin: bool = False) -> dict:
    result = {"id": row["id"], "name": row.get("name", "Basketball game"),
            "status": row.get("status", "uploaded"), "progress": row.get("progress") or {},
            "error": row.get("error"), "spend_usd": float(row.get("spend_usd", 0) or 0),
            "created_at": isoformat(row.get("created_at")),
            "event_count": int(row.get("event_count", 0)),
            "included_count": int(row.get("included_count", 0)),
            "processed_seconds": float(row["processed_seconds"]) if row.get("processed_seconds") is not None else None,
            "run_of": row.get("run_of"),
            "performance": (row.get("media_metrics") or {}).get("performance"),
            "media_expires_at": isoformat(row.get("media_expires_at")),
            "media_available": media.available(row),
            "reel_job": {k: v for k, v in (row.get("reel_job") or {}).items()
                         if k in {"id", "status", "format", "combine_overlaps", "exclude_free_throws", "error"}},
            "latest_reel": ({**{k: v for k, v in row["latest_reel"].items() if k != "key"},
                             "generated_at": isoformat(row["latest_reel"].get("generated_at"))}
                            if row.get("latest_reel") else None)}
    result["is_admin"] = admin
    if not admin:
        result.pop("spend_usd", None)
        result.pop("performance", None)
        result["progress"] = {k: v for k, v in result["progress"].items()
                              if k in {"phase", "completed", "total", "context_completed", "context_total",
                                       "stage_completed", "stage_total", "clips_completed", "clips_total",
                                       "uploads_completed", "uploads_total"}}
    return result


def credit_packs():
    if settings.stripe_credit_packs:
        try:
            packs = json.loads(settings.stripe_credit_packs)
            parsed = {str(k): {"price_id": str(v["price_id"]), "minutes": int(v["minutes"])}
                      for k, v in packs.items()}
            if any(not value["price_id"] or value["minutes"] <= 0 for value in parsed.values()):
                raise ValueError("each pack must have a Stripe Price ID and positive minutes")
            return parsed
        except Exception as exc:
            raise RuntimeError("STRIPE_CREDIT_PACKS must map pack ids to price_id and minutes") from exc
    return {"60": {"price_id": settings.stripe_price_60_minutes, "minutes": 60}} if settings.stripe_price_60_minutes else {}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def public_config():
    return {"mode": "hosted", "configured": settings.configured_auth,
            "firebase": {"apiKey": settings.firebase_api_key,
                         "authDomain": settings.firebase_auth_domain or f"{settings.firebase_project_id}.firebaseapp.com",
                         "projectId": settings.firebase_project_id,
                         "appId": settings.firebase_app_id}}


@app.get("/", include_in_schema=False)
def home():
    return seo.html(FRONTEND, "/")


@app.get("/api/account")
def account(request: Request, user_id: str = Depends(current_user)):
    if billing.enabled():
        return {**billing.account(user_id), "is_admin": is_admin(request)}
    from google.cloud import firestore

    ref = account_ref(user_id)
    transaction = client().transaction()

    @firestore.transactional
    def load_or_create(tx):
        snapshot = next(tx.get(ref))
        if snapshot.exists:
            return snapshot.to_dict() or {}
        initial = {"available_milli": 0, "reserved_milli": 0, "updated_at": server_timestamp()}
        tx.set(ref, initial)
        return initial

    data = load_or_create(transaction)
    return {"available_minutes": minutes_from_milli(int(data.get("available_milli", 0)) + int(data.get("legacy_available_milli", 0))),
            "reserved_minutes": minutes_from_milli(data.get("reserved_milli", 0)), "is_admin": is_admin(request)}


@app.get("/api/runs")
def list_runs(request: Request, user_id: str = Depends(current_user)):
    rows = [run_summary({"id": item.id, **(item.to_dict() or {})}, admin=is_admin(request)) for item in
            runs_ref(user_id).order_by("created_at", direction="DESCENDING").stream()]
    return rows


@app.get("/api/demo")
def demo_summary(user_id: str = Depends(current_user)):
    return demo.summary(demo.load())


@app.get("/api/demo/game")
def demo_game(user_id: str = Depends(current_user)):
    return demo.detail(demo.load())


def check_analysis_credits(user_id: str, duration_seconds: float):
    required = milli(Decimal(str(duration_seconds)) / 60)
    if billing.enabled():
        balance, sub = billing.load(user_id)
        billing.require_minutes(balance, required)
        if duration_seconds / 60 > billing.entitlement(balance, sub)["max_minutes"]:
            raise HTTPException(402, "Free uploads are limited to 15 minutes. Upgrade to analyze a full game.")
    else:
        balance = account_ref(user_id).get().to_dict() or {}
        buckets = ("legacy_available_milli", "available_milli") if balance.get("billing_migrated") else ("available_milli",)
        billing.require_minutes(balance, required, buckets)


@app.post("/api/runs/upload")
def create_upload(body: UploadRequest, user_id: str = Depends(current_user)):
    extension = Path(body.file_name).suffix.lower()
    if extension not in SUPPORTED:
        raise HTTPException(400, "Unsupported video type")
    if body.size_bytes > settings.max_upload_bytes:
        raise HTTPException(413, "Video exceeds the configured upload limit")
    if body.duration_seconds / 60 > settings.max_reserved_minutes:
        raise HTTPException(413, "Video exceeds the maximum run duration")
    check_analysis_credits(user_id, body.duration_seconds)
    run_id = str(uuid.uuid4())
    object_key = storage.key(user_id, run_id, f"source{extension}")
    content_type = "video/mp4" if extension == ".mp4" else "application/octet-stream"
    try:
        signed = storage.signed_upload(user_id, run_id, extension, content_type, body.size_bytes)
    except Exception as exc:
        raise HTTPException(503, "Video storage is unavailable") from exc
    now = server_timestamp()
    run_ref(user_id, run_id).set({"id": run_id, "user_id": user_id, "name": Path(body.name).name,
        "source_key": object_key, "source_bytes": body.size_bytes,
        "declared_duration_seconds": body.duration_seconds, "reserved_milli": 0,
        "processed_seconds": None, "status": "uploaded", "progress": {}, "error": None,
        "spend_usd": 0.0, "event_count": 0, "included_count": 0, "run_of": None,
        "created_at": now, "updated_at": now, "media_expires_at": utcnow() + media.RETENTION, "retention_version": 1})
    return {"id": run_id, "upload_url": signed["upload_url"], "expires_in": signed["expires_in"],
            "required_headers": {"Content-Type": content_type}}


@app.post("/api/runs/{run_id}/start", status_code=202)
def start_run(run_id: str, user_id: str = Depends(current_user)):
    run, data = owned_run(user_id, run_id)
    media.require_media(data)
    if data.get("status") == "queued":
        box = outbox_ref(run_id).get().to_dict() or {}
        try:
            tasks.enqueue(run_id, int(box.get("dispatch_attempt", 0)))
        except Exception as exc:
            raise HTTPException(503, "Analysis is queued and will be retried automatically") from exc
        return {"id": run_id, "status": "queued", "already_started": True}
    if data.get("status") in {"running", "cancel_requested", "complete"}:
        return {"id": run_id, "status": data["status"], "already_started": True}
    if data.get("status") in {"failed", "cancelled"}:
        raise HTTPException(409, "This run has ended. Use Run again to start a fresh analysis.")
    try:
        head = storage.client().head_object(Bucket=settings.r2_bucket, Key=data["source_key"])
    except Exception as exc:
        raise HTTPException(409, "Upload the video before starting analysis") from exc
    if head.get("ContentLength") != data.get("source_bytes"):
        raise HTTPException(409, "Uploaded video size does not match the upload request")
    reserve = milli(Decimal(str(data["declared_duration_seconds"])) / 60)
    subscriptions = billing.enabled()
    account = account_ref(user_id)
    ledger = ledger_ref(user_id, f"run:{run_id}:reserve")
    outbox = outbox_ref(run_id)
    transaction = client().transaction()

    from google.cloud import firestore

    @firestore.transactional
    def reserve_credits(tx):
        current = next(tx.get(run)).to_dict() or {}
        if current.get("status") == "queued":
            return "queued", int((next(tx.get(outbox)).to_dict() or {}).get("dispatch_attempt", 0))
        media.require_media(current)
        if current.get("status") != "uploaded":
            raise HTTPException(409, "Run is no longer available to start")
        balance = next(tx.get(account)).to_dict() or {"available_milli": 0, "reserved_milli": 0}
        sub = next(tx.get(billing.subscription_ref(user_id))).to_dict() or {} if subscriptions else {}
        try:
            if subscriptions:
                updated, funding, access = billing.reserve(balance, sub, reserve, current["declared_duration_seconds"] / 60)
            elif balance.get("billing_migrated"):
                updated, funding = billing.reserve_prepaid(balance, reserve)
                access = {"max_minutes": settings.max_reserved_minutes}
            else:
                billing.require_minutes(balance, reserve, ("available_milli",))
                available = int(balance.get("available_milli", 0))
                updated = {**balance, "available_milli": available - reserve,
                           "reserved_milli": int(balance.get("reserved_milli", 0)) + reserve}
                funding, access = {"available_milli": reserve}, {"max_minutes": settings.max_reserved_minutes}
        except HTTPException as exc:
            if exc.status_code != 402:
                raise
            # Commit the rejection before returning HTTP 402; throwing inside
            # the transaction would leave the draft incorrectly marked uploaded.
            tx.update(run, {"status": "failed", "error": exc.detail, "updated_at": server_timestamp()})
            return "rejected", exc.detail
        if subscriptions:
            billing.record_allowance(tx, user_id, balance, updated)
        tx.set(account, {**updated, "updated_at": server_timestamp()}, merge=True)
        tx.set(ledger, {"event_key": f"run:{run_id}:reserve", "run_id": run_id, "kind": "reserve",
                        "available_delta_milli": -reserve, "reserved_delta_milli": reserve,
                        "funding": funding, "description": f"Reserved {minutes_from_milli(reserve):g} video minutes",
                        "created_at": server_timestamp()})
        tx.update(run, {"status": "queued", "reserved_milli": reserve, "updated_at": server_timestamp(),
                        "analysis_queued_at": server_timestamp(),
                        "analysis_settings": current.get("analysis_settings") or analysis_settings(settings),
                        "error": None, "funding": funding, "analysis_authorization": access,
                        "legacy_access": bool(current.get("legacy_access") or funding.get("legacy_available_milli", 0))})
        tx.set(outbox, {"run_id": run_id, "user_id": user_id, "dispatch_attempt": 0,
                        "created_at": server_timestamp(), "dispatched_at": None})
        return "queued", 0

    status, attempt = reserve_credits(transaction)
    if status == "rejected":
        raise HTTPException(402, attempt)
    try:
        tasks.enqueue(run_id, attempt)
    except Exception as exc:
        raise HTTPException(503, "Analysis is queued and will be retried automatically") from exc
    return {"id": run_id, "status": status, "reserved_minutes": minutes_from_milli(reserve)}


@app.post("/api/runs/{run_id}/rerun", status_code=201)
def rerun(run_id: str, user_id: str = Depends(current_user)):
    from google.cloud import firestore
    old_ref, existing = owned_run(user_id, run_id)
    media.require_media(existing)
    check_analysis_credits(user_id, existing["declared_duration_seconds"])
    new_id = str(uuid.uuid4())
    tx = client().transaction()

    @firestore.transactional
    def hold(transaction):
        previous = next(transaction.get(old_ref)).to_dict() or {}
        media.require_media(previous)
        if previous.get("status") in media.ACTIVE_ANALYSIS or media.busy(previous):
            raise HTTPException(409, "Wait for the current processing job to finish")
        transaction.update(old_ref, {"media_hold_until": utcnow() + timedelta(minutes=10), "media_hold_token": new_id})
        return previous

    previous = hold(tx)
    new_ref = run_ref(user_id, new_id)
    try:
        extension = Path(previous["source_key"]).suffix
        new_key = storage.key(user_id, new_id, f"source{extension}")
        now = server_timestamp()
        # Copy only analysis inputs, never another run's reel, sponsors, leases, or expiry.
        data = {"id": new_id, "user_id": user_id, "name": previous.get("name", "Game"),
                "source_key": new_key, "source_bytes": previous["source_bytes"],
                "declared_duration_seconds": previous["declared_duration_seconds"],
                "status": "uploaded", "reserved_milli": 0, "processed_seconds": None,
                "progress": {}, "error": None, "spend_usd": 0.0, "event_count": 0,
                "included_count": 0, "run_of": run_id, "lineage_id": previous.get("lineage_id") or run_id,
                "legacy_access": bool(previous.get("legacy_access")), "created_at": now, "updated_at": now,
                "media_expires_at": utcnow() + media.RETENTION, "retention_version": 1}
        storage.client().copy_object(Bucket=settings.r2_bucket, Key=new_key,
                                     CopySource={"Bucket": settings.r2_bucket, "Key": previous["source_key"]})
        new_ref.set(data)
    except Exception as exc:
        raise HTTPException(503, "Could not copy the saved source video") from exc
    finally:
        tx = client().transaction()

        @firestore.transactional
        def release(transaction):
            current = next(transaction.get(old_ref)).to_dict() or {}
            if current.get("media_hold_token") == new_id:
                transaction.update(old_ref, {"media_hold_until": None, "media_hold_token": None})
        release(tx)
    return {"id": new_id, "status": "uploaded"}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, request: Request, user_id: str = Depends(current_user)):
    _, data = owned_run(user_id, run_id)
    events = read_events(user_id, run_id)
    result = run_summary(data, admin=is_admin(request))
    if billing.enabled():
        balance, sub = billing.load(user_id)
        result["entitlements"] = billing.entitlement(balance, sub, data)
        result["has_saved_csv"] = data.get("csv_snapshot") is not None
        usage = billing.usage_ref(user_id, data.get("lineage_id") or run_id).get().to_dict() or {}
        result["reels_used"] = int(usage.get("successful", 0))
    result["events"] = []
    for event in events:
        payload = dict(event.get("payload") or {})
        payload["included"] = bool(event.get("included", False))
        clip_key = event.get("clip_key")
        payload["clip_url"] = media.clip_download(clip_key, data) if clip_key else None
        result["events"].append(payload)
    result["sponsors"] = [{"id": item["id"], "name": item["name"], "url": media.clip_download(item["key"], data)}
                          for item in data.get("sponsors", [])] if media.available(data) else []
    return result


@app.patch("/api/runs/{run_id}/events/{event_id}")
def set_selection(run_id: str, request: Request, event_id: str, body: Selection, user_id: str = Depends(current_user)):
    run, _ = owned_run(user_id, run_id)
    ref = event_ref(user_id, run_id, event_id)
    transaction = client().transaction()

    from google.cloud import firestore

    @firestore.transactional
    def update_selection(tx):
        run_data = next(tx.get(run)).to_dict() or {}
        event_data = next(tx.get(ref)).to_dict()
        if event_data is None:
            raise HTTPException(404, "Clip not found")
        previous = bool(event_data.get("included", False))
        if previous == body.included:
            return
        count = int(run_data.get("included_count", 0)) + (1 if body.included else -1)
        tx.update(ref, {"included": body.included})
        tx.update(run, {"included_count": max(0, count), "updated_at": server_timestamp()})

    update_selection(transaction)
    return get_run(run_id, request, user_id)


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, user_id: str = Depends(current_user)):
    run, _ = owned_run(user_id, run_id)
    account = account_ref(user_id)
    cancel_ledger = ledger_ref(user_id, f"run:{run_id}:cancel")
    transaction = client().transaction()

    from google.cloud import firestore

    @firestore.transactional
    def cancel(tx):
        data = next(tx.get(run)).to_dict() or {}
        status = data.get("status")
        if status == "queued":
            reserve = int(data.get("reserved_milli", 0))
            balance = next(tx.get(account)).to_dict() or {"available_milli": 0, "reserved_milli": 0}
            if not next(tx.get(cancel_ledger)).exists:
                released = billing.release(balance, data, 0, utcnow())
                refund = sum(int(released.get(key, 0)) - int(balance.get(key, 0)) for key in billing.BALANCES)
                tx.set(account, {**released, "updated_at": server_timestamp()}, merge=True)
                tx.set(cancel_ledger, {"event_key": f"run:{run_id}:cancel", "run_id": run_id,
                    "kind": "release", "available_delta_milli": refund, "reserved_delta_milli": -reserve,
                    "description": "Released credits for cancelled queued run", "created_at": server_timestamp()})
            tx.update(run, {"status": "cancelled", "updated_at": server_timestamp(), "reserved_milli": 0})
        elif status == "running":
            tx.update(run, {"status": "cancel_requested", "updated_at": server_timestamp()})
        elif status in {"uploaded", "failed"}:
            tx.update(run, {"status": "cancelled", "updated_at": server_timestamp()})
        return status

    cancel(transaction)
    latest = run.get().to_dict() or {}
    return {"id": run_id, "status": latest.get("status")}


def csv_text(rows):
    output = io.StringIO(newline="")
    if rows:
        first = dict(rows[0].get("payload") or {})
        first["included"] = bool(rows[0].get("included"))
        writer = csv.DictWriter(output, fieldnames=list(first))
        writer.writeheader()
        for item in rows:
            row = dict(item.get("payload") or {})
            row["included"] = bool(item.get("included"))
            for field in ("evidence_timestamps", "source_windows"):
                if isinstance(row.get(field), (list, dict)):
                    row[field] = json.dumps(row[field])
            writer.writerow(row)
    else:
        output.write("event_id,timestamp_seconds,classification,included\n")
    return output.getvalue()


@app.get("/api/runs/{run_id}/csv")
def download_csv(run_id: str, user_id: str = Depends(current_user)):
    ref, data = owned_run(user_id, run_id)
    subscriptions = billing.enabled()
    from google.cloud import firestore
    @firestore.transactional
    def export(tx):
        current = next(tx.get(ref)).to_dict() or {}
        if subscriptions:
            current_balance = next(tx.get(account_ref(user_id))).to_dict() or {}
            current_sub = next(tx.get(billing.subscription_ref(user_id))).to_dict() or {}
            allowed = billing.entitlement(current_balance, current_sub, current)["csv"]
            if not allowed:
                saved = current.get("csv_snapshot")
                if saved is None:
                    raise HTTPException(402, "CSV export is included in Pro and Business. Upgrade to export these selections.")
                return zlib.decompress(saved).decode("utf-8") if isinstance(saved, bytes) else saved
        rows = [item.to_dict() or {} for item in tx.get(events_ref(user_id, run_id).order_by("__name__"))]
        rows.sort(key=lambda row: float(row.get("timestamp_seconds", 0)))
        text = csv_text(rows)
        tx.update(ref, {"csv_snapshot": zlib.compress(text.encode("utf-8")), "csv_exported_at": utcnow()})
        return text
    if subscriptions:
        text = export(client().transaction())
    else:
        text = csv_text(read_events(user_id, run_id))
        ref.update({"csv_snapshot": zlib.compress(text.encode("utf-8")), "csv_exported_at": utcnow()})
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", data.get("name", "game"))[:160]
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}-events.csv"'})


@app.post("/api/runs/{run_id}/reel", status_code=202)
def build_reel(run_id: str, body: ReelRequest = ReelRequest(), user_id: str = Depends(current_user)):
    owned_run(user_id, run_id)
    return reels.queue(user_id, run_id, body.format, body.sponsor_ids, body.combine_overlaps, body.exclude_free_throws)


@app.get("/api/runs/{run_id}/reel/preview")
def preview_reel(run_id: str, user_id: str = Depends(current_user)):
    _, data = owned_run(user_id, run_id)
    latest = data.get("latest_reel")
    if not latest:
        raise HTTPException(404, "No saved reel is available")
    return {"url": storage.signed_download(latest["key"], expires=3600), "count": latest.get("count", 0)}


@app.get("/api/runs/{run_id}/reel/download")
def download_reel(run_id: str, user_id: str = Depends(current_user)):
    _, data = owned_run(user_id, run_id)
    latest = data.get("latest_reel")
    if not latest:
        raise HTTPException(404, "No saved reel is available")
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", data.get("name", "game"))[:160] + "-reel.mp4"
    return {"url": storage.signed_download(latest["key"], filename=filename), "count": latest.get("count", 0)}


@app.post("/api/runs/{run_id}/sponsors", status_code=201)
async def upload_sponsor(run_id: str, request: Request, file: UploadFile = File(...), user_id: str = Depends(current_user)):
    from google.cloud import firestore
    ref, data = owned_run(user_id, run_id)
    if billing.enabled():
        balance, sub = billing.load(user_id)
        if not billing.entitlement(balance, sub, data)["sponsors"]:
            raise HTTPException(402, "Sponsor end cards are included in Business. Upgrade to add logos.")
    from basketball_highlights.social_reel import MAX_LOGO_BYTES, validate_logo
    media.require_media(data)
    raw = await file.read(MAX_LOGO_BYTES + 1)
    try:
        normalized = validate_logo(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    sponsor_id = str(uuid.uuid4())
    key = storage.key(user_id, run_id, f"sponsors/{sponsor_id}.png")
    storage.client().put_object(Bucket=settings.r2_bucket, Key=key, Body=normalized, ContentType="image/png")
    tx = client().transaction()

    @firestore.transactional
    def add(transaction):
        current = next(transaction.get(ref)).to_dict()
        if current is None:
            raise HTTPException(404, "Run not found")
        media.require_media(current)
        if settings.subscriptions_enabled:
            balance = next(transaction.get(account_ref(user_id))).to_dict() or {}
            sub = next(transaction.get(billing.subscription_ref(user_id))).to_dict() or {}
            if not billing.entitlement(balance, sub, current)["sponsors"]:
                raise HTTPException(402, "Sponsor end cards require Business")
        if media.busy(current):
            raise HTTPException(409, "Wait for the reel to finish before changing sponsors")
        sponsors = current.get("sponsors", [])
        if len(sponsors) >= 2:
            raise HTTPException(400, "Remove a sponsor before uploading another; maximum two logos")
        transaction.update(ref, {"sponsors": [*sponsors, {"id": sponsor_id, "key": key,
                                                                      "name": Path(file.filename or "Sponsor").name[:255]}]})
    try:
        add(tx)
    except Exception:
        storage.delete_object(key)
        raise
    return get_run(run_id, request, user_id)


@app.delete("/api/runs/{run_id}/sponsors/{sponsor_id}")
def remove_sponsor(run_id: str, request: Request, sponsor_id: str, user_id: str = Depends(current_user)):
    from google.cloud import firestore
    ref, _ = owned_run(user_id, run_id)
    tx = client().transaction()

    @firestore.transactional
    def remove(transaction):
        data = next(transaction.get(ref)).to_dict() or {}
        media.require_media(data)
        if media.busy(data):
            raise HTTPException(409, "Wait for the reel to finish before changing sponsors")
        sponsors = data.get("sponsors", [])
        match = next((item for item in sponsors if item["id"] == sponsor_id), None)
        if match is None:
            raise HTTPException(404, "Sponsor not found")
        transaction.update(ref, {"sponsors": [item for item in sponsors if item["id"] != sponsor_id]})
        return match["key"]
    key = remove(tx)
    storage.delete_object(key)
    return get_run(run_id, request, user_id)


@app.delete("/api/runs/{run_id}", status_code=204)
def delete_run(run_id: str, user_id: str = Depends(current_user)):
    run, data = owned_run(user_id, run_id)
    from google.cloud import firestore
    tx = client().transaction()

    @firestore.transactional
    def deleting(transaction):
        current = next(transaction.get(run)).to_dict() or {}
        if current.get("media_hold_until") and current["media_hold_until"] > utcnow():
            remaining = max(1, math.ceil((current["media_hold_until"] - utcnow()).total_seconds() / 60))
            raise HTTPException(409, f"Run again is copying this game's video. Try deleting it after copying finishes, or in {remaining} minute{'s' if remaining != 1 else ''} if the copy was interrupted.")
        if current.get("status") in media.ACTIVE_ANALYSIS or media.busy(current):
            raise HTTPException(409, "Wait for processing to stop before deleting this game")
        transaction.update(run, {"media_cleanup_started_at": utcnow()})
    deleting(tx)
    try:
        storage.delete_prefix(f"users/{user_id}/runs/{run_id}/")
    except Exception as exc:
        raise HTTPException(503, "Media deletion failed. Try deleting this game again.") from exc
    for event in events_ref(user_id, run_id).stream():
        event.reference.delete()
    run.delete()
    outbox_ref(run_id).delete()
    return Response(status_code=204)


@app.get("/api/billing/packs")
def billing_packs(user_id: str = Depends(current_user)):
    if billing.enabled():
        return [{key: billing.TOPUP[key] for key in ("id", "minutes", "price_usd")}] if billing.account(user_id)["can_top_up"] else []
    return [{"id": key, "minutes": value["minutes"]} for key, value in credit_packs().items()]


@app.post("/api/billing/checkout")
def checkout(body: CheckoutRequest, user_id: str = Depends(current_user)):
    if billing.enabled():
        if body.pack_key != billing.TOPUP["id"]:
            raise HTTPException(400, "Unknown credit pack")
        return billing.topup(user_id)
    import stripe
    pack = credit_packs().get(body.pack_key)
    if not pack:
        raise HTTPException(400, "Unknown credit pack")
    if not (settings.stripe_secret_key and pack["price_id"]):
        raise HTTPException(503, "Stripe credit pack is not configured")
    if billing.stripe_live_mode():
        raise HTTPException(503, "Live billing requires the subscription offering")
    stripe = billing.stripe_client()
    session = stripe.checkout.Session.create(mode="payment", line_items=[{"price": pack["price_id"], "quantity": 1}],
        success_url=f"{settings.app_base_url}/?payment=success", cancel_url=f"{settings.app_base_url}/?payment=cancelled",
        client_reference_id=user_id, metadata={"user_id": user_id, "pack_key": body.pack_key})
    return {"url": session.url}


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(default="")):
    import stripe
    if not settings.stripe_webhook_secret:
        raise HTTPException(503, "Stripe webhook is not configured")
    try:
        event = billing.stripe_data(stripe.Webhook.construct_event(await request.body(), stripe_signature, settings.stripe_webhook_secret))
    except Exception as exc:
        raise HTTPException(400, "Invalid Stripe webhook") from exc
    billing.require_event_mode(event)
    obj = event["data"]["object"]
    subscription_event = (event["type"].startswith(("customer.subscription.", "subscription_schedule.")) or
                          event["type"] in {"invoice.paid", "invoice.payment_failed", "invoice.payment_action_required"} or
                          obj.get("mode") == "subscription" or obj.get("metadata", {}).get("kind") == "topup")
    if subscription_event:
        await run_in_threadpool(billing.webhook, event)
        return {"received": True}
    if settings.subscriptions_enabled:
        billing.enabled()
    if event["type"] in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        session = event["data"]["object"]
        if session.get("payment_status") == "paid":
            user_id = session.get("metadata", {}).get("user_id")
            session_id = session["id"]
            pack = credit_packs().get(session.get("metadata", {}).get("pack_key", ""))
            if not user_id or session.get("client_reference_id") != user_id or not pack:
                raise HTTPException(400, "Checkout session metadata is invalid")
            account = account_ref(user_id)
            receipt = ledger_ref(user_id, f"stripe:{session_id}")
            transaction = client().transaction()
            from google.cloud import firestore

            @firestore.transactional
            def grant_credits(tx):
                if next(tx.get(receipt)).exists:
                    return False
                current = next(tx.get(account)).to_dict() or {"available_milli": 0, "reserved_milli": 0}
                amount = pack["minutes"] * 1000
                bucket = "legacy_available_milli" if current.get("billing_migrated") else "available_milli"
                tx.set(account, {bucket: int(current.get(bucket, 0)) + amount,
                                 "reserved_milli": int(current.get("reserved_milli", 0)),
                                 "updated_at": server_timestamp()}, merge=True)
                tx.set(receipt, {"event_key": f"stripe:{session_id}", "kind": "purchase",
                    "available_delta_milli": amount, "reserved_delta_milli": 0,
                    "stripe_session_id": session_id, "description": f"Purchased {pack['minutes']} video minutes",
                    "created_at": server_timestamp()})
                return True

            grant_credits(transaction)
    return {"received": True}


def verify_internal(authorization: str = Header(default="")):
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token
        scheme, token = authorization.split(" ", 1)
        if scheme.lower() != "bearer":
            raise ValueError("missing bearer")
        claims = id_token.verify_oauth2_token(token, GoogleRequest(), audience=settings.internal_audience)
        if claims.get("email") != settings.dispatch_service_account:
            raise ValueError("wrong service account")
    except Exception as exc:
        raise HTTPException(403, "Forbidden") from exc


@app.post("/internal/dispatch")
def internal_dispatch(body: dict, _identity=Depends(verify_internal)):
    try:
        run_id = str(uuid.UUID(str(body["run_id"])))
    except Exception as exc:
        raise HTTPException(400, "Invalid run id") from exc
    box_ref = outbox_ref(run_id)
    box = box_ref.get().to_dict()
    if not box:
        return {"skipped": run_id}
    user_id = box["user_id"]
    run_data = read_run(user_id, run_id)
    if not run_data or run_data.get("status") != "queued":
        box_ref.set({"dispatched_at": server_timestamp()}, merge=True)
        return {"skipped": run_id}
    tasks.launch_job(run_id, user_id)
    box_ref.set({"dispatched_at": server_timestamp()}, merge=True)
    return {"launched": run_id}


@app.post("/internal/dispatch-pending")
def dispatch_pending(_identity=Depends(verify_internal)):
    allowances_refreshed = billing.refresh_all()
    reel_recovery = reels.recover()
    db = client()
    now = utcnow()
    cutoff = now - timedelta(minutes=15)
    recovered = 0
    checked = 0
    enqueued = 0
    active = db.collection_group("runs").where("status", "in", ["queued", "running", "cancel_requested"]).stream()
    for snapshot in active:
        data = snapshot.to_dict() or {}
        user_id = data.get("user_id")
        run_id = snapshot.id
        if not user_id:
            continue
        checked += 1
        ref = snapshot.reference
        box_ref = outbox_ref(run_id)
        box = box_ref.get().to_dict() or {}
        status = data.get("status")
        dispatch_attempt = int(box.get("dispatch_attempt", 0))
        if status == "queued" and not box:
            box = {"run_id": run_id, "user_id": user_id, "dispatch_attempt": 0,
                   "dispatched_at": None, "created_at": server_timestamp()}
            box_ref.set(box, merge=True)
        if status in {"running", "cancel_requested"}:
            lease = data.get("lease_expires_at")
            if lease and lease > now:
                continue
            if data.get("updated_at") and data["updated_at"] > cutoff:
                continue

            from hosted.worker import recover_run
            if recover_run(user_id, run_id, data.get("worker_token"), expired=True,
                           reason="Worker execution stopped"):
                recovered += 1
            # Recovery queues through the same outbox, or settles a terminal run.
            continue
        elif status == "queued" and box.get("dispatched_at") is not None:
            dispatched_at = box.get("dispatched_at")
            if dispatched_at > now - timedelta(minutes=10):
                continue
            from google.cloud import firestore
            tx = db.transaction()

            @firestore.transactional
            def recover_dispatch(transaction):
                current_run = next(transaction.get(ref)).to_dict() or {}
                current_box = next(transaction.get(box_ref)).to_dict() or {}
                sent_at = current_box.get("dispatched_at")
                if current_run.get("status") != "queued" or not sent_at or sent_at > now - timedelta(minutes=10):
                    return False
                transaction.set(box_ref, {"dispatch_attempt": int(current_box.get("dispatch_attempt", 0)) + 1,
                    "dispatched_at": None}, merge=True)
                return True

            if not recover_dispatch(tx):
                continue
            dispatch_attempt += 1
            recovered += 1
        try:
            tasks.enqueue(run_id, dispatch_attempt)
            enqueued += 1
        except Exception:
            continue
    return {"queued": enqueued, "checked": checked, "recovered": recovered, "reels": reel_recovery, "allowances_refreshed": allowances_refreshed}


@app.get("/api/billing/plans")
def pricing_plans():
    return {"enabled": billing.enabled(), "currency": "usd", "plans": [{"id": key, **value} for key, value in billing.PLANS.items()]}


@app.post("/api/billing/subscription/checkout")
def subscription_checkout(body: SubscriptionRequest, user_id: str = Depends(current_user)):
    if not billing.enabled():
        raise HTTPException(503, "Subscriptions are not available yet")
    return billing.checkout(user_id, body.plan, body.interval)


@app.post("/api/billing/portal")
def subscription_portal(user_id: str = Depends(current_user)):
    if not billing.enabled():
        raise HTTPException(503, "Subscriptions are not available yet")
    return billing.portal(user_id)


@app.post("/api/billing/subscription/preview")
def subscription_preview(body: SubscriptionRequest, user_id: str = Depends(current_user)):
    if not billing.enabled():
        raise HTTPException(503, "Subscriptions are not available yet")
    return billing.upgrade_preview(user_id, body.plan, body.interval)


@app.post("/api/billing/subscription/change")
def subscription_change(body: SubscriptionRequest, user_id: str = Depends(current_user)):
    if not billing.enabled():
        raise HTTPException(503, "Subscriptions are not available yet")
    return billing.schedule_change(user_id, body.plan, body.interval, body.quote_id)


@app.post("/internal/billing/live-transition")
def live_transition(body: LiveTransitionRequest = LiveTransitionRequest(), _identity=Depends(verify_internal)):
    return billing.transition_live(verify=body.verify, dry_run=body.dry_run)


@app.post("/internal/billing/migrate")
def migrate_billing(body: MigrationRequest = MigrationRequest(), _identity=Depends(verify_internal)):
    return billing.migrate(body.verify)


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    return seo.robots()


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    return seo.sitemap(FRONTEND)


@app.get("/pricing", include_in_schema=False)
def pricing_redirect():
    return RedirectResponse("/#pricing", status_code=308)


@app.get("/{path:path}", include_in_schema=False)
def frontend_route(path: str):
    if path.startswith(("api/", "internal/", "assets/")):
        raise HTTPException(404, "Not found")
    route = "/" + path
    known = route in seo.pages(FRONTEND) or route in {"/sign-in", "/account", "/library", "/demo"} or bool(re.fullmatch(r"/runs/[^/]+", route))
    return seo.html(FRONTEND, route, status_code=200 if known else 404)


@app.post("/internal/media/migrate")
def migrate_media(body: MigrationRequest = MigrationRequest(), _identity=Depends(verify_internal)):
    return media.migrate(body.verify)


@app.post("/internal/media/cleanup")
def cleanup_media(_identity=Depends(verify_internal)):
    return media.cleanup()
