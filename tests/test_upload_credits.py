"""Reject unaffordable analyses before upload and keep race failures truthful."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from hosted.app import billing, db as refs, main
from test_billing import env, paid
from test_media import RUN


@pytest.mark.parametrize("subscriptions,migrated", [(True, True), (False, True), (False, False)])
def test_insufficient_minutes_create_no_upload_or_run(env, monkeypatch, subscriptions, migrated):
    config = replace(main.settings, subscriptions_enabled=subscriptions)
    monkeypatch.setattr(main, "settings", config)
    monkeypatch.setattr(billing, "settings", config)
    balance = {"billing_migrated": migrated, "free_granted": True, "free_available_milli": 0, "subscription_available_milli": 0,
               "available_milli": 1000, "legacy_available_milli": 1000 if migrated else 0, "reserved_milli": 5000}
    billing.account_ref("alice").set(balance)
    before = env[0].rows.copy()
    def unexpected(*args, **kwargs):
        raise AssertionError("Storage must not be touched for rejected uploads")
    monkeypatch.setattr(main.storage, "signed_upload", unexpected)
    with TestClient(main.app) as api:
        response = api.post("/api/runs/upload", json={"name": "Game", "file_name": "game.mp4",
            "size_bytes": 100, "duration_seconds": 600})
    assert response.status_code == 402
    assert "Not enough credits" in response.json()["detail"]
    assert "10 video minutes" in response.json()["detail"]
    assert env[0].rows == before


@pytest.mark.parametrize("subscriptions,migrated", [(True, True), (False, True), (False, False)])
def test_affordable_upload_does_not_reserve_until_start(env, monkeypatch, subscriptions, migrated):
    config = replace(main.settings, subscriptions_enabled=subscriptions)
    monkeypatch.setattr(main, "settings", config)
    monkeypatch.setattr(billing, "settings", config)
    billing.account_ref("alice").set({"billing_migrated": migrated, "free_granted": True,
        "free_available_milli": 0, "subscription_available_milli": 0, "available_milli": 10000, "reserved_milli": 0})
    before = billing.account_ref("alice").get().to_dict()
    with TestClient(main.app) as api:
        response = api.post("/api/runs/upload", json={"name": "Game", "file_name": "game.mp4",
            "size_bytes": 100, "duration_seconds": 600})
        assert response.status_code == 200
        new_id = response.json()["id"]
        assert billing.account_ref("alice").get().to_dict() == before
        assert api.post(f"/api/runs/{new_id}/start").status_code == 202
    balance = billing.account_ref("alice").get().to_dict()
    assert balance["available_milli"] == 0 and balance["reserved_milli"] == 10000


@pytest.mark.parametrize("subscriptions,migrated", [(True, True), (False, True), (False, False)])
def test_credit_race_marks_run_failed_without_reserving_or_queueing(env, monkeypatch, subscriptions, migrated):
    config = replace(main.settings, subscriptions_enabled=subscriptions)
    monkeypatch.setattr(main, "settings", config)
    monkeypatch.setattr(billing, "settings", config)
    billing.account_ref("alice").set({"billing_migrated": migrated, "free_granted": True,
        "free_available_milli": 0, "subscription_available_milli": 0, "available_milli": 10000, "reserved_milli": 0})
    with TestClient(main.app) as api:
        response = api.post("/api/runs/upload", json={"name": "Game", "file_name": "game.mp4",
            "size_bytes": 100, "duration_seconds": 600})
        run_id = response.json()["id"]
        # Another analysis uses the remaining credits while this file uploads.
        billing.account_ref("alice").update({"available_milli": 0, "reserved_milli": 10000})
        before = billing.account_ref("alice").get().to_dict()
        monkeypatch.setattr(main.tasks, "enqueue", lambda *_: pytest.fail("Rejected analysis must not enqueue"))
        rejected = api.post(f"/api/runs/{run_id}/start")
        assert rejected.status_code == 402
        run = refs.run_ref("alice", run_id).get().to_dict()
        assert run["status"] == "failed" and run["reserved_milli"] == 0
        assert run["error"] == rejected.json()["detail"]
        assert api.post(f"/api/runs/{run_id}/start").status_code == 409
    assert billing.account_ref("alice").get().to_dict() == before
    assert not refs.outbox_ref(run_id).get().exists
    assert not refs.ledger_ref("alice", f"run:{run_id}:reserve").get().exists


def test_fractional_duration_uses_same_rounded_reservation_as_start(env):
    billing.account_ref("alice").set({"billing_migrated": True, "free_granted": True,
        "free_available_milli": 0, "subscription_available_milli": 0, "available_milli": 1000, "reserved_milli": 0})
    with TestClient(main.app) as api:
        response = api.post("/api/runs/upload", json={"name": "Game", "file_name": "game.mp4",
            "size_bytes": 100, "duration_seconds": 60.001})
    assert response.status_code == 402 and "1.001 video minutes" in response.json()["detail"]


def test_rejected_rerun_creates_no_draft_or_media_hold(env, monkeypatch):
    billing.account_ref("alice").set({"billing_migrated": True, "free_granted": True,
        "free_available_milli": 0, "subscription_available_milli": 0, "available_milli": 0, "reserved_milli": 0})
    before = env[0].rows.copy()
    with TestClient(main.app) as api:
        response = api.post(f"/api/runs/{RUN}/rerun")
    assert response.status_code == 402 and env[0].rows == before


def test_paid_monthly_allowance_is_refreshed_before_upload_check(env):
    billing.subscription_ref("alice").set(paid())
    with TestClient(main.app) as api:
        response = api.post("/api/runs/upload", json={"name": "Game", "file_name": "game.mp4",
            "size_bytes": 100, "duration_seconds": 5400})
    assert response.status_code == 200
    assert billing.account("alice")["subscription_minutes"] == 360
