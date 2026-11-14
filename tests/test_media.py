from __future__ import annotations

import copy
from dataclasses import replace
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from hosted.app import media, reels, main, billing
from basketball_highlights.social_reel import validate_logo, MAX_LOGO_BYTES

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
RUN = "a7c0c3aa-6af9-4d7c-8c54-2523fd65025b"


class Snapshot:
    def __init__(self, ref):
        self.reference = ref
        self.id = ref.id
        self.exists = ref.path in ref.db.rows
        self.data = copy.deepcopy(ref.db.rows.get(ref.path))
    def to_dict(self):
        return self.data


class Ref:
    def __init__(self, db, path):
        self.db, self.path, self.id = db, path, path.split("/")[-1]
    def get(self):
        return Snapshot(self)
    def set(self, value, merge=False):
        self.db.rows[self.path] = {**(self.db.rows.get(self.path, {}) if merge else {}), **copy.deepcopy(value)}
    def update(self, value):
        if not value:
            raise ValueError("Cannot update with an empty document.")
        if self.path not in self.db.rows:
            raise ValueError("Document missing")
        self.db.rows[self.path].update(copy.deepcopy(value))
    def collection(self, name):
        return Collection(self.db, self.path + "/" + name)
    def delete(self):
        self.db.rows.pop(self.path, None)


class Collection:
    def __init__(self, db, path):
        self.db, self.path = db, path
    def document(self, name):
        return Ref(self.db, self.path + "/" + name)
    def order_by(self, field):
        return Query(self.db, self.path)
    def stream(self):
        return iter([Snapshot(Ref(self.db, path)) for path in self.db.rows
                     if path.startswith(self.path + "/") and path.count("/") == self.path.count("/") + 1])


class Query(Collection):
    pass


class Transaction:
    def get(self, ref):
        if isinstance(ref, Ref):
            return iter([ref.get()])
        if isinstance(ref, Query):
            return ref.stream()
        raise ValueError("Transaction.get requires a document reference or query")
    def update(self, ref, data):
        ref.update(data)
    def set(self, ref, data, merge=False):
        ref.set(data, merge)


class Database:
    def __init__(self):
        self.rows = {}
    def collection(self, name):
        return Collection(self, name)
    def transaction(self):
        return Transaction()
    def collection_group(self, name):
        return SimpleNamespace(stream=lambda: iter([Snapshot(Ref(self, path)) for path in list(self.rows)
                                                    if path.split("/")[-2] == name]))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(billing, "settings", replace(billing.settings, stripe_mode="test", stripe_live_enabled=False, subscriptions_enabled=False))
    import google.cloud.firestore
    monkeypatch.setattr(google.cloud.firestore, "transactional", lambda fn: fn)
    db = Database()
    ref = db.collection("users").document("alice").collection("runs").document(RUN)
    ref.set({"user_id": "alice", "status": "complete", "source_key": f"users/alice/runs/{RUN}/source.mp4",
             "media_expires_at": NOW + timedelta(days=14), "created_at": NOW, "included_count": 1})
    ref.collection("events").document("e1").set({"included": True, "timestamp_seconds": 5,
        "payload": {"clip_start_seconds": 2, "clip_end_seconds": 8}})
    for module in (media, reels, main):
        monkeypatch.setattr(module, "client", lambda: db)
    monkeypatch.setattr(reels, "run_ref", lambda user, run: db.collection("users").document(user).collection("runs").document(run))
    monkeypatch.setattr(reels, "events_ref", lambda user, run: db.collection("users").document(user).collection("runs").document(run).collection("events"))
    monkeypatch.setattr(main, "run_ref", reels.run_ref)
    monkeypatch.setattr(main, "read_events", lambda user, run: [s.to_dict() for s in reels.events_ref(user, run).stream()])
    monkeypatch.setattr(main, "server_timestamp", lambda: NOW)
    for module in (media, reels, main):
        monkeypatch.setattr(module, "utcnow", lambda: NOW)
    monkeypatch.setattr(reels.tasks, "launch_reel_job", lambda *args: None)
    monkeypatch.setattr(media.storage, "signed_download", lambda key, expires=600, **kwargs: f"signed:{key}:{expires}")
    return db, ref


def test_expiry_boundary_and_links(env):
    _, ref = env
    data = ref.get().to_dict()
    data["media_expires_at"] = NOW + timedelta(seconds=42)
    assert media.clip_download("clip", data) == "signed:clip:42"
    data["media_expires_at"] = NOW
    assert not media.available(data)
    assert media.clip_download("clip", data) is None
    with pytest.raises(HTTPException) as error:
        media.require_media(data)
    assert error.value.status_code == 410
    data["status"] = "running"
    assert media.available(data)


def test_queue_snapshots_and_duplicate_prevention(env):
    _, ref = env
    result = reels.queue("alice", RUN, "vertical", [])
    assert result["status"] == "queued"
    ref.collection("events").document("e1").update({"included": False})
    assert ref.get().to_dict()["reel_job"]["windows"] == [{"start": 2, "end": 8}]
    with pytest.raises(HTTPException) as error:
        reels.queue("alice", RUN, "landscape", [])
    assert error.value.status_code == 409


def test_overlap_export_does_not_reuse_old_repeated_footage(env):
    _, ref = env
    ref.collection("events").document("e2").set({"included": True, "timestamp_seconds": 7,
        "payload": {"clip_start_seconds": 4.0, "clip_end_seconds": 10.0}})
    old_windows = [{"start": 2.0, "end": 8.0}, {"start": 4.0, "end": 10.0}]
    ref.update({"latest_reel": {"key": "old-overlapping-reel", "job_id": "old",
                               "fingerprint": billing.reel_fingerprint(old_windows, "landscape", [])}})
    result = reels.queue("alice", RUN, "landscape", [])
    assert result["status"] == "queued"
    assert result["count"] == 2
    job = ref.get().to_dict()["reel_job"]
    assert job["fingerprint"] == billing.reel_fingerprint([{"start": 2.0, "end": 10.0}], "landscape", [])
    assert len(job["windows"]) == 2  # Selection snapshot is retained for the worker.
    assert all(row.to_dict()["included"] for row in ref.collection("events").stream())


def test_queue_rejects_expired_foreign_sponsors_and_empty_selection(env):
    _, ref = env
    with pytest.raises(HTTPException) as error:
        reels.queue("alice", RUN, "vertical", ["someone-elses-logo"])
    assert error.value.status_code == 400
    ref.collection("events").document("e1").update({"included": False})
    with pytest.raises(HTTPException):
        reels.queue("alice", RUN, "vertical", [])
    ref.update({"media_expires_at": NOW})
    with pytest.raises(HTTPException) as error:
        reels.queue("alice", RUN, "vertical", [])
    assert error.value.status_code == 410


def fake_storage(monkeypatch, fail_delete=False):
    objects, deleted = set(), []
    def download(bucket, key, path):
        from pathlib import Path
        Path(path).write_bytes(b"original")
    def upload(path, bucket, key, **kwargs):
        objects.add(key)
    def delete(key):
        if fail_delete:
            raise RuntimeError("R2 unavailable")
        deleted.append(key)
        objects.discard(key)
    s3 = SimpleNamespace(download_file=download, upload_file=upload,
        get_paginator=lambda _: SimpleNamespace(paginate=lambda **kwargs: [{"Contents": [{"Key": key} for key in objects]}]))
    monkeypatch.setattr(media.storage, "client", lambda: s3)
    monkeypatch.setattr(media.storage, "delete_object", delete)
    monkeypatch.setattr(media.storage, "exists", lambda key: key in objects)
    return objects, deleted


@pytest.mark.parametrize("combine", [True, False])
def test_original_source_publish_and_duplicate_worker(env, monkeypatch, combine):
    _, ref = env
    objects, deleted = fake_storage(monkeypatch)
    ref.update({"latest_reel": {"key": "old-reel", "count": 1}})
    objects.add("old-reel")
    job = reels.queue("alice", RUN, "vertical", [], combine_overlaps=combine)
    assert ref.get().to_dict()["reel_job"]["combine_overlaps"] is combine
    calls = []
    def render(source, windows, target, format, logos, *, combine_overlaps=True):
        assert combine_overlaps is combine
        assert source.read_bytes() == b"original"
        assert windows == [{"start": 2, "end": 8}]
        calls.append(source)
        target.write_bytes(b"reel")
        return {"count": 1, "format": format}
    monkeypatch.setattr(reels, "render_reel", render)
    reels.run("alice", RUN, job["id"])
    reels.run("alice", RUN, job["id"])
    assert len(calls) == 1
    assert ref.get().to_dict()["reel_job"]["status"] == "ready"
    assert deleted == ["old-reel"]
    assert ref.get().to_dict()["latest_reel"]["key"] in objects


def test_switching_overlap_mode_does_not_reuse_other_mode(env):
    _, ref = env
    ref.collection("events").document("e1").update({"payload": {"clip_start_seconds": 2.0, "clip_end_seconds": 8.0}})
    windows = [{"start": 2.0, "end": 8.0}]
    for combine in [True, False]:
        ref.update({"reel_job": None, "latest_reel": {"key": "saved", "job_id": "saved",
            "fingerprint": billing.reel_fingerprint(windows, "vertical", [], combine)}})
        assert reels.queue("alice", RUN, "vertical", [], combine_overlaps=combine)["status"] == "ready"
        assert reels.queue("alice", RUN, "vertical", [], combine_overlaps=not combine)["status"] == "queued"


def test_hosted_api_forwards_overlap_choice_and_defaults_to_combine(env, monkeypatch):
    _, ref = env
    calls = []
    monkeypatch.setattr(main, "owned_run", lambda *args: ref.get().to_dict())
    monkeypatch.setattr(reels, "queue", lambda *args: calls.append(args) or {"status": "queued", "count": 1})
    main.app.dependency_overrides[main.current_user] = lambda: "alice"
    try:
        with TestClient(main.app) as api:
            assert api.post(f"/api/runs/{RUN}/reel", json={"combine_overlaps": False}).status_code == 202
            assert calls[-1][-2:] == (False, True)
            assert api.post(f"/api/runs/{RUN}/reel", json={}).status_code == 202
            assert calls[-1][-2:] == (True, True)
            assert api.post(f"/api/runs/{RUN}/reel", json={"exclude_free_throws": True}).status_code == 202
            assert calls[-1][-2:] == (True, True)
            assert api.post(f"/api/runs/{RUN}/reel", json={"exclude_free_throws": False}).status_code == 202
            assert calls[-1][-2:] == (True, False)
    finally:
        main.app.dependency_overrides.pop(main.current_user, None)


def test_failed_export_preserves_previous(env, monkeypatch):
    _, ref = env
    objects, deleted = fake_storage(monkeypatch)
    ref.update({"latest_reel": {"key": "old-reel"}})
    objects.add("old-reel")
    job = reels.queue("alice", RUN, "vertical", [])
    monkeypatch.setattr(reels, "render_reel", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("FFmpeg failed")))
    with pytest.raises(RuntimeError):
        reels.run("alice", RUN, job["id"])
    assert ref.get().to_dict()["latest_reel"]["key"] == "old-reel"
    assert ref.get().to_dict()["reel_job"]["status"] == "failed"
    assert not deleted


def test_cleanup_waits_for_export_and_retry_keeps_latest(env, monkeypatch):
    db, ref = env
    db.collection("maintenance").document("media-retention-v1").set({"verified": True})
    monkeypatch.setattr(media, "settings", SimpleNamespace(media_cleanup_enabled=True, r2_bucket="test"))
    objects, deleted = fake_storage(monkeypatch, fail_delete=True)
    objects.update({"source.mp4", "clips/e1.mp4", "sponsors/logo.png", "latest.mp4", "reels/old.mp4"})
    job = reels.queue("alice", RUN, "vertical", [])
    ref.update({"media_expires_at": NOW, "latest_reel": {"key": "latest.mp4"}})
    assert media.cleanup()["deleted"] == 0
    ref.update({"reel_job": {"id": job["id"], "status": "rendering", "lease_expires_at": NOW}})
    assert media.cleanup()["failed"] == 1
    assert ref.get().to_dict().get("media_cleanup_started_at")
    monkeypatch.setattr(media.storage, "delete_object", lambda key: (objects.discard(key), deleted.append(key)))
    assert media.cleanup()["deleted"] == 1
    assert objects == {"latest.mp4"}
    assert not media.available(ref.get().to_dict())
    assert media.cleanup()["deleted"] == 0
    assert ref.collection("events").document("e1").get().exists


def test_cleanup_gate_and_legacy_recovery(env, monkeypatch):
    db, ref = env
    ref.set({"user_id": "alice", "status": "complete", "created_at": NOW - timedelta(days=90), "included_count": 3})
    objects, _ = fake_storage(monkeypatch)
    legacy = media.storage.key("alice", RUN, "highlights_reel.mp4")
    objects.add(legacy)
    monkeypatch.setattr(media, "settings", SimpleNamespace(media_cleanup_enabled=True, r2_bucket="test"))
    assert media.cleanup()["enabled"] is False
    assert media.migrate()["recovered_reels"] == 1
    data = ref.get().to_dict()
    assert data["media_expires_at"] == NOW + timedelta(days=14)
    assert data["latest_reel"]["key"] == legacy
    monkeypatch.setattr(media, "utcnow", lambda: NOW + timedelta(days=1))
    assert media.migrate(True)["verified"]
    assert ref.get().to_dict()["media_expires_at"] == data["media_expires_at"]


def test_stale_worker_cannot_publish(env, monkeypatch):
    _, ref = env
    objects, deleted = fake_storage(monkeypatch)
    ref.update({"latest_reel": {"key": "previous"}})
    job = reels.queue("alice", RUN, "vertical", [])
    def render(source, windows, target, format, logos, *, combine_overlaps=True):
        target.write_bytes(b"reel")
        ref.update({"reel_job": {"status": "failed", "id": job["id"]}})
        return {"count": 1}
    monkeypatch.setattr(reels, "render_reel", render)
    reels.run("alice", RUN, job["id"])
    assert ref.get().to_dict()["latest_reel"]["key"] == "previous"
    assert not objects
    assert len(deleted) == 1


def test_api_ownership_expired_csv_and_download(env):
    _, ref = env
    ref.update({"name": "Game", "media_expires_at": NOW, "latest_reel": {"key": "saved", "count": 1, "format": "vertical"}})
    main.app.dependency_overrides[main.current_user] = lambda: "alice"
    try:
        with TestClient(main.app) as api:
            detail = api.get(f"/api/runs/{RUN}").json()
            assert detail["media_available"] is False
            assert detail["events"][0]["clip_url"] is None
            assert api.get(f"/api/runs/{RUN}/reel/download").json()["url"] == "signed:saved:600"
            assert api.get(f"/api/runs/{RUN}/csv").status_code == 200
            assert api.post(f"/api/runs/{RUN}/reel", json={}).status_code == 410
            main.app.dependency_overrides[main.current_user] = lambda: "bob"
            assert api.get(f"/api/runs/{RUN}").status_code == 404
            assert api.get(f"/api/runs/{RUN}/reel/download").status_code == 404
            assert api.post(f"/api/runs/{RUN}/sponsors", files={"file": ("logo.png", b"bad", "image/png")}).status_code == 404
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_logo_normalization(format):
    raw = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(raw, format=format)
    result = Image.open(io.BytesIO(validate_logo(raw.getvalue())))
    assert result.format == "PNG"
    assert result.size == (200, 100)


def test_logo_transparency_and_rejections():
    raw = io.BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 0)).save(raw, format="PNG")
    assert Image.open(io.BytesIO(validate_logo(raw.getvalue()))).getpixel((0, 0))[3] == 0
    for invalid in (b"not an image", b"x" * (MAX_LOGO_BYTES + 1), b"<svg/>"):
        with pytest.raises(ValueError):
            validate_logo(invalid)


def test_sponsor_api_validation_limit_and_expired_access(env, monkeypatch):
    _, ref = env
    objects, deleted = fake_storage(monkeypatch)
    s3 = media.storage.client()
    s3.put_object = lambda **kwargs: objects.add(kwargs["Key"])
    raw = io.BytesIO()
    Image.new("RGBA", (30, 20), (255, 255, 255, 0)).save(raw, format="PNG")
    main.app.dependency_overrides[main.current_user] = lambda: "alice"
    try:
        with TestClient(main.app) as api:
            url = f"/api/runs/{RUN}/sponsors"
            assert api.post(url, files={"file": ("bad.png", b"corrupt", "image/png")}).status_code == 400
            for index in range(2):
                response = api.post(url, files={"file": (f"sponsor{index}.png", raw.getvalue(), "image/png")})
                assert response.status_code == 201
                assert len(response.json()["sponsors"]) == index + 1
            assert api.post(url, files={"file": ("third.png", raw.getvalue(), "image/png")}).status_code == 400
            assert len(objects) == 2  # failed uploads do not leave unreferenced logo objects
            sponsor = ref.get().to_dict()["sponsors"][0]
            response = api.delete(url + "/" + sponsor["id"])
            assert response.status_code == 200
            assert len(response.json()["sponsors"]) == 1
            ref.update({"media_expires_at": NOW})
            assert api.post(url, files={"file": ("late.png", raw.getvalue(), "image/png")}).status_code == 410
            assert api.post(f"/api/runs/{RUN}/rerun").status_code == 410
            assert api.post("/internal/media/cleanup").status_code == 403
            assert api.post("/internal/media/migrate", json={}).status_code == 403
    finally:
        main.app.dependency_overrides.clear()


def test_dispatch_recovery_and_expired_render_lease(env, monkeypatch):
    _, ref = env
    calls = []
    monkeypatch.setattr(reels.tasks, "launch_reel_job", lambda *args: calls.append(args))
    job = reels.queue("alice", RUN, "vertical", [])
    assert len(calls) == 1
    monkeypatch.setattr(reels, "utcnow", lambda: NOW + timedelta(minutes=16))
    assert reels.recover()["launched"] == 1
    assert len(calls) == 2
    assert reels.recover()["launched"] == 0
    ref.update({"latest_reel": {"key": "previous"}, "reel_job": {"id": job["id"], "status": "rendering", "lease_expires_at": NOW}})
    assert reels.recover()["timed_out"] == 1
    assert ref.get().to_dict()["latest_reel"]["key"] == "previous"


def test_first_migration_pass_cannot_enable_deletion(env, monkeypatch):
    db, ref = env
    ref.set({"user_id": "alice", "status": "complete"})
    fake_storage(monkeypatch)
    assert not media.migrate(True)["verified"]
    assert not db.collection("maintenance").document("media-retention-v1").get().to_dict()["verified"]
    assert media.migrate(True)["verified"]


def test_superseded_reel_deletion_retries_before_footage_expiry(env, monkeypatch):
    db, ref = env
    objects, deleted = fake_storage(monkeypatch, fail_delete=True)
    objects.add("old-reel")
    ref.update({"latest_reel": {"key": "old-reel"}})
    job = reels.queue("alice", RUN, "landscape", [])
    def render(source, windows, target, format, logos, *, combine_overlaps=True):
        target.write_bytes(b"reel")
        return {"count": 1, "format": format}
    monkeypatch.setattr(reels, "render_reel", render)
    reels.run("alice", RUN, job["id"])
    assert ref.get().to_dict()["reel_job"]["status"] == "ready"
    assert ref.get().to_dict()["reel_delete_pending"] == ["old-reel"]
    db.collection("maintenance").document("media-retention-v1").set({"verified": True})
    monkeypatch.setattr(media, "settings", SimpleNamespace(media_cleanup_enabled=True, r2_bucket="test"))
    monkeypatch.setattr(media.storage, "delete_object", lambda key: objects.discard(key))
    assert media.cleanup()["failed"] == 0
    assert ref.get().to_dict()["reel_delete_pending"] == []
    assert "old-reel" not in objects
    assert ref.get().to_dict()["latest_reel"]["key"] in objects
    assert media.available(ref.get().to_dict())


def test_reel_download_signs_an_attachment_filename(monkeypatch):
    captured = {}
    def sign(operation, **kwargs):
        captured.update(kwargs)
        return "download"
    monkeypatch.setattr(media.storage, "client", lambda: SimpleNamespace(generate_presigned_url=sign))
    assert media.storage.signed_download("saved.mp4", filename="game-reel.mp4") == "download"
    assert captured["Params"]["ResponseContentDisposition"] == 'attachment; filename="game-reel.mp4"'
    assert captured["ExpiresIn"] == 600


@pytest.mark.parametrize("finish_status, days", [("complete", 14), ("cancelled", 14), ("failed", 13)])
def test_settlement_deadline_and_credit_accounting(env, monkeypatch, finish_status, days):
    from decimal import Decimal
    from hosted import worker
    db, ref = env
    ref.update({"status": "running", "worker_token": "owner", "created_at": NOW - timedelta(days=1)})
    account = db.collection("users").document("alice").collection("account").document("credits")
    account.set({"available_milli": 0, "reserved_milli": 2000})
    monkeypatch.setattr(worker, "client", lambda: db)
    monkeypatch.setattr(worker, "run_ref", lambda *args: ref)
    monkeypatch.setattr(worker, "account_ref", lambda *args: account)
    monkeypatch.setattr(worker, "ledger_ref", lambda user, key: db.collection("ledger").document(key))
    monkeypatch.setattr(worker, "utcnow", lambda: NOW)
    monkeypatch.setattr(worker, "server_timestamp", lambda: NOW)
    worker.settle("alice", RUN, "owner", 2000, Decimal(60), 0.5, finish_status, None)
    data = ref.get().to_dict()
    assert data["media_expires_at"] == NOW + timedelta(days=days)
    assert data["processed_seconds"] == 30
    assert data["status"] == finish_status
    assert account.get().to_dict() == {"available_milli": 1500, "reserved_milli": 0, "updated_at": NOW}


def test_delete_storage_failure_can_be_retried(env, monkeypatch):
    db, ref = env
    monkeypatch.setattr(main, "events_ref", reels.events_ref)
    monkeypatch.setattr(main, "outbox_ref", lambda id: db.collection("outbox").document(id))
    monkeypatch.setattr(media.storage, "delete_prefix", lambda prefix: (_ for _ in ()).throw(RuntimeError("R2 unavailable")))
    main.app.dependency_overrides[main.current_user] = lambda: "alice"
    try:
        with TestClient(main.app) as api:
            assert api.delete(f"/api/runs/{RUN}").status_code == 503
            assert ref.get().exists
            assert not media.available(ref.get().to_dict())
            assert api.post(f"/api/runs/{RUN}/reel", json={}).status_code == 410
            monkeypatch.setattr(media.storage, "delete_prefix", lambda prefix: None)
            assert api.delete(f"/api/runs/{RUN}").status_code == 204
            assert not ref.get().exists
    finally:
        main.app.dependency_overrides.clear()


def test_delete_reports_rerun_hold_and_allows_retry_after_expiry(env, monkeypatch):
    db, ref = env
    ref.update({"status": "cancelled", "media_hold_until": NOW + timedelta(minutes=4)})
    monkeypatch.setattr(main, "utcnow", lambda: NOW)
    monkeypatch.setattr(main, "events_ref", reels.events_ref)
    monkeypatch.setattr(main, "outbox_ref", lambda id: db.collection("outbox").document(id))
    deletes = []
    monkeypatch.setattr(media.storage, "delete_prefix", lambda prefix: deletes.append(prefix))
    main.app.dependency_overrides[main.current_user] = lambda: "alice"
    try:
        with TestClient(main.app) as api:
            response = api.delete(f"/api/runs/{RUN}")
            assert response.status_code == 409
            assert "Run again" in response.json()["detail"]
            assert "4 minutes" in response.json()["detail"]
            assert not deletes and ref.get().exists
            ref.update({"media_hold_until": NOW - timedelta(seconds=1)})
            assert api.delete(f"/api/runs/{RUN}").status_code == 204
            assert len(deletes) == 1 and not ref.get().exists
    finally:
        main.app.dependency_overrides.clear()


def test_firestore_sdk_accepts_event_query_not_collection(monkeypatch):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import Client, Query as FirestoreQuery
    sdk = Client(project="offline-contract-test", credentials=AnonymousCredentials())
    events = sdk.collection("users").document("alice").collection("runs").document(RUN).collection("events")
    tx = sdk.transaction()
    with pytest.raises(ValueError, match="DocumentReference or a Query"):
        tx.get(events)
    captured = []
    def stream(query, **kwargs):
        captured.append(kwargs["transaction"])
        return iter([])
    monkeypatch.setattr(FirestoreQuery, "stream", stream)
    assert list(tx.get(events.order_by("__name__"))) == []
    assert captured == [tx]
