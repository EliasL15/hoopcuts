"""Costs are private API data and admin status comes from verified identity."""
from fastapi import Request
from fastapi.testclient import TestClient
import pytest

from hosted.app import auth, main, db
from test_billing import env
from test_media import RUN, Collection


@pytest.mark.parametrize("email,verified,expected", [
    ("elias.liassides@gmail.com", True, True),
    ("ELIAS.LIASSIDES@gmail.com", True, True),
    ("elias.liassides@gmail.com", False, False),
    ("other@gmail.com", True, False),
    (None, True, False),
])
def test_admin_identity(email, verified, expected):
    assert auth.admin_claims({"email": email, "email_verified": verified, "admin": True}) is expected


@pytest.mark.parametrize("admin", [False, True])
def test_account_and_run_responses_restrict_costs(env, admin, monkeypatch):
    monkeypatch.setattr(Collection, "order_by", lambda self, *args, **kwargs: self)
    def identity(request: Request):
        request.state.is_admin = admin
        return "alice"
    main.app.dependency_overrides[main.current_user] = identity
    db.run_ref("alice", RUN).update({"spend_usd": .256,
        "progress": {"phase": "analyzing", "completed": 2, "total": 10, "spend_usd": .2,
                     "remaining_usd": 4, "estimated_compute_usd": .01, "active_requests": 4},
        "media_metrics": {"performance": {"estimated_compute_usd": .0166}}})
    with TestClient(main.app) as api:
        assert api.get("/api/account").json()["is_admin"] is admin
        responses = [api.get("/api/runs").json()[0], api.get(f"/api/runs/{RUN}").json(),
                     api.patch(f"/api/runs/{RUN}/events/e1", json={"included": False}).json()]
    for result in responses:
        assert result["is_admin"] is admin
        assert result["progress"]["completed"] == 2
        if admin:
            assert result["spend_usd"] == .256
            assert result["performance"]["estimated_compute_usd"] == .0166
            assert result["progress"]["remaining_usd"] == 4
        else:
            assert "spend_usd" not in result and "performance" not in result
            assert set(result["progress"]) == {"phase", "completed", "total"}


def test_customer_progress_shows_pass_counts_without_internal_metrics():
    result = main.run_summary({"id": "example", "status": "running", "progress": {
        "phase": "creating_clips", "completed": 100, "total": 100,
        "stage_completed": 3, "stage_total": 10, "clips_completed": 3, "clips_total": 10,
        "uploads_completed": 2, "uploads_total": 3, "active_requests": 4,
        "spend_usd": 1.2, "estimated_compute_usd": .05}}, admin=False)
    assert result["progress"]["clips_completed"] == 3
    assert result["progress"]["stage_total"] == 10
    assert result["progress"]["uploads_completed"] == 2
    assert not {"spend_usd", "estimated_compute_usd", "active_requests"}.intersection(result["progress"])
