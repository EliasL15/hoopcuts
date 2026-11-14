"""Live billing must explicitly activate and isolate historical sandbox grants."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hosted.app import billing, main
from test_billing import env, stripe, paid, event


def live(monkeypatch, *, active=True, maintenance=False):
    config = replace(billing.settings, stripe_mode="live", stripe_live_enabled=active,
                     stripe_secret_key="sk_live_fixture", billing_migration_mode=maintenance, app_base_url="https://hoopcuts.example")
    monkeypatch.setattr(billing, "settings", config)
    monkeypatch.setattr(main, "settings", config)
    return config


def test_key_mode_is_explicit_and_live_needs_activation(env, monkeypatch):
    import stripe as sdk
    config = live(monkeypatch, active=False)
    with pytest.raises(HTTPException):
        billing.stripe_client()
    monkeypatch.setattr(billing, "settings", replace(config, stripe_live_enabled=True))
    assert billing.stripe_client() is sdk
    for mode, key in [("test", "sk_live_fixture"), ("live", "sk_test_fixture"), ("invalid", "sk_test_fixture")]:
        monkeypatch.setattr(billing, "settings", replace(config, stripe_mode=mode, stripe_secret_key=key))
        with pytest.raises(HTTPException):
            billing.stripe_client()


def test_live_price_portal_and_topup_cannot_use_sandbox_objects(env, stripe, monkeypatch):
    live(monkeypatch)
    billing.client().collection("maintenance").document("stripe-live-v1").set({"verified": True})
    billing.account_ref("alice").set({"stripe_mode": "live"}, merge=True)
    billing.subscription_ref("alice").set(paid())
    with pytest.raises(HTTPException):
        billing.checked_price("pro", "monthly")
    with pytest.raises(HTTPException):
        billing.portal("alice")
    with pytest.raises(HTTPException):
        billing.topup("alice")
    for value in stripe.prices.values():
        value["livemode"] = True
    stripe.config["livemode"] = True
    assert billing.checked_price("pro", "monthly") == "price_pro_m"
    assert billing.portal("alice")["url"]
    assert billing.topup("alice")["url"]


def test_live_transition_blocks_activation_and_legacy_billing_fallback(env, monkeypatch):
    config = live(monkeypatch)
    with pytest.raises(HTTPException):
        billing.enabled()
    monkeypatch.setattr(billing, "settings", replace(config, subscriptions_enabled=False))
    with pytest.raises(HTTPException):
        billing.enabled()


def test_transition_dry_run_archives_once_preserves_legacy_and_trial_history(env, monkeypatch):
    live(monkeypatch, active=False, maintenance=True)
    ref = billing.account_ref("alice")
    ref.set({"subscription_available_milli": 360000, "available_milli": 60000,
             "legacy_available_milli": 3179, "free_granted": True, "free_available_milli": 7000,
             "reserved_milli": 0, "allowance_key": "test-sub:window", "expired_subscription_milli": 12000}, merge=True)
    before = ref.get().to_dict()
    oldsub = billing.client().collection("users").document("alice").collection("account").document("subscription")
    oldsub.set(paid())
    dry = billing.transition_live(dry_run=True)
    assert dry["sandbox_subscription_milli"] == 360000 and dry["sandbox_prepaid_milli"] == 60000
    assert ref.get().to_dict() == before
    assert not billing.client().collection("maintenance").document("stripe-live-v1").get().exists
    assert billing.transition_live()["changed"] == 1
    updated = ref.get().to_dict()
    assert updated["stripe_mode"] == "live"
    assert updated["subscription_available_milli"] == updated["available_milli"] == updated["expired_subscription_milli"] == 0
    assert updated["legacy_available_milli"] == 3179 and updated["free_available_milli"] == 7000
    assert updated["free_granted"] is True
    assert ref.collection("environment_archive").document("stripe-test-v1").get().to_dict()["credits"] == before
    assert billing.transition_live()["changed"] == 0
    assert billing.transition_live(verify=True)["verified"] is True
    assert billing.subscription_ref("alice").get().to_dict() is None
    assert oldsub.get().to_dict() == paid()
    # Activation cannot grant another free trial or restore sandbox paid access.
    monkeypatch.setattr(billing, "settings", replace(billing.settings, stripe_live_enabled=True, billing_migration_mode=False))
    account = billing.account("alice")
    assert account["plan"] == "free" and account["free_minutes"] == 7
    assert account["legacy_minutes"] == 3.179
    assert billing.enabled() is True


@pytest.mark.parametrize("field,value", [("reserved_milli", 1000), ("status", "running")])
def test_transition_refuses_reservations_or_active_jobs(env, monkeypatch, field, value):
    live(monkeypatch, active=False, maintenance=True)
    if field == "reserved_milli":
        billing.account_ref("alice").set({field: value}, merge=True)
    else:
        env[1].set({field: value}, merge=True)
    with pytest.raises(HTTPException):
        billing.transition_live()
    assert billing.account_ref("alice").get().to_dict().get("stripe_mode") is None


def test_live_refresh_ignores_test_subscription_and_grants_live_once(env, monkeypatch):
    live(monkeypatch)
    billing.account_ref("alice").set({"stripe_mode": "live", "free_granted": True, "free_available_milli": 0}, merge=True)
    old = billing.client().collection("users").document("alice").collection("account").document("subscription")
    old.set(paid(plan="business"))
    assert billing.account("alice")["plan"] == "free"
    assert billing.account("alice")["subscription_minutes"] == 0
    billing.subscription_ref("alice").set({**paid(), "subscription_id": "sub_live"})
    assert billing.account("alice")["subscription_minutes"] == 360
    assert billing.account("alice")["subscription_minutes"] == 360
    # Accidentally rolling back Stripe mode cannot consume live balances.
    monkeypatch.setattr(billing, "settings", replace(billing.settings, stripe_mode="test"))
    with pytest.raises(HTTPException):
        billing.account("alice")


def test_webhook_mode_mismatch_is_rejected_before_resource_access(env, stripe, monkeypatch):
    import stripe as sdk
    live(monkeypatch)
    monkeypatch.setattr(sdk.Webhook, "construct_event", lambda *args: event())
    with TestClient(main.app) as client:
        response = client.post("/api/stripe/webhook", content=b"fixture")
        assert response.status_code == 400
    with pytest.raises(HTTPException):
        billing.webhook(event())


def test_live_verified_webhook_projects_paid_live_subscription(env, stripe, monkeypatch):
    live(monkeypatch)
    billing.client().collection("maintenance").document("stripe-live-v1").set({"verified": True})
    billing.account_ref("alice").set({"stripe_mode": "live"}, merge=True)
    billing.subscription_ref("alice").set({"customer_id": "cus_alice"})
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    stripe.subscriptions["sub_alice"]["livemode"] = True
    billing.webhook({**event(), "livemode": True})
    assert billing.account("alice")["plan"] == "pro"
    assert billing.account("alice")["subscription_minutes"] == 360
    billing.webhook({**event(eid="evt_reordered_live"), "livemode": True})
    assert billing.account("alice")["subscription_minutes"] == 360


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://hoopcuts.example", "https://hoopcuts.example/private"])
def test_live_payments_refuse_invalid_public_origins(env, monkeypatch, url):
    config = live(monkeypatch)
    monkeypatch.setattr(billing, "settings", replace(config, app_base_url=url))
    with pytest.raises(HTTPException):
        billing.stripe_client()
