"""Billing behavior with atomic, read-before-write Firestore and Stripe doubles."""
from __future__ import annotations

import copy
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from hosted.app import billing, db as refs, main, reels
from hosted import worker
from test_media import Database, Transaction, NOW, RUN


class AtomicTransaction(Transaction):
    def __init__(self, db):
        self.db, self.wrote = db, False
    def get(self, ref):
        if self.wrote:
            raise AssertionError("Firestore reads must precede writes")
        return super().get(ref)
    def set(self, *args, **kwargs):
        self.wrote = True
        return super().set(*args, **kwargs)
    def update(self, *args, **kwargs):
        self.wrote = True
        return super().update(*args, **kwargs)


@pytest.fixture
def env(monkeypatch):
    import google.cloud.firestore
    store = Database()
    mutex = threading.RLock()
    store.transaction = lambda: AtomicTransaction(store)
    def transactional(fn):
        def call(tx, *args, **kwargs):
            with mutex:
                previous = copy.deepcopy(store.rows)
                try:
                    return fn(tx, *args, **kwargs)
                except Exception:
                    store.rows = previous
                    raise
        return call
    monkeypatch.setattr(google.cloud.firestore, "transactional", transactional)
    config = replace(main.settings, subscriptions_enabled=True, billing_migration_mode=False,
                     analysis_group_windows=1, stripe_mode="test", stripe_live_enabled=False,
                     stripe_secret_key="sk_test_fixture", stripe_webhook_secret="whsec_fixture",
                     stripe_price_pro_monthly="price_pro_m", stripe_price_pro_annual="price_pro_y",
                     stripe_price_business_monthly="price_business_m", stripe_price_business_annual="price_business_y",
                     stripe_price_topup="price_topup", stripe_portal_configuration_id="bpc_test")
    for module in (billing, refs, main, reels, worker):
        monkeypatch.setattr(module, "client", lambda: store)
        monkeypatch.setattr(module, "utcnow", lambda: NOW)
        if hasattr(module, "settings"):
            monkeypatch.setattr(module, "settings", config)
    for module in (main, worker):
        monkeypatch.setattr(module, "server_timestamp", lambda: NOW)
    store.collection("maintenance").document("subscriptions-v1").set({"verified": True})
    billing.account_ref("alice").set({"available_milli": 0, "reserved_milli": 0, "billing_migrated": True})
    game = refs.run_ref("alice", RUN)
    game.set({"user_id": "alice", "name": "Game", "status": "complete", "source_key": "source.mp4",
              "source_bytes": 100, "declared_duration_seconds": 3600, "created_at": NOW,
              "media_expires_at": NOW + timedelta(days=14), "included_count": 1})
    refs.events_ref("alice", RUN).document("e1").set({"included": True, "timestamp_seconds": 5,
        "payload": {"event_id": "e1", "timestamp_seconds": 5, "clip_start_seconds": 2, "clip_end_seconds": 8}})
    monkeypatch.setattr(reels.tasks, "launch_reel_job", lambda *args: None)
    monkeypatch.setattr(main.tasks, "enqueue", lambda *args: None)
    monkeypatch.setattr(main.storage, "signed_download", lambda key, **kwargs: "signed:" + key)
    monkeypatch.setattr(main.storage, "signed_upload", lambda *args: {"upload_url": "signed:upload", "expires_in": 60})
    monkeypatch.setattr(main.storage, "client", lambda: SimpleNamespace(head_object=lambda **kw: {"ContentLength": 100}))
    main.app.dependency_overrides[main.current_user] = lambda: "alice"
    main.app.dependency_overrides[main.verify_internal] = lambda: True
    yield store, game
    main.app.dependency_overrides.clear()


def paid(plan="pro", interval="monthly", start=NOW, end=None):
    return {"customer_id": "cus_alice", "subscription_id": "sub_alice", "plan": plan, "paid_plan": plan,
            "interval": interval, "status": "active", "paid_until": end or NOW + timedelta(days=30),
            "current_period_end": end or NOW + timedelta(days=30), "allowance_anchor": start}


def test_free_once_and_account_isolation(env):
    assert billing.account("alice")["free_minutes"] == 15
    assert billing.account("alice")["free_minutes"] == 15
    balance, _ = billing.load("alice")
    updated, _, _ = billing.reserve(balance, {}, 10000, 10)
    billing.account_ref("alice").set(updated)
    assert billing.account("alice")["free_minutes"] == 5
    assert billing.account("bob")["free_minutes"] == 15


def test_annual_month_end_and_skipped_periods(env, monkeypatch):
    anchor = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
    sub = paid(interval="annual", start=anchor, end=datetime(2027, 1, 31, 12, tzinfo=timezone.utc))
    billing.subscription_ref("alice").set(sub)
    monkeypatch.setattr(billing, "utcnow", lambda: datetime(2026, 2, 28, 12, tzinfo=timezone.utc))
    first = billing.account("alice")
    assert first["subscription_minutes"] == 360
    assert first["allowance_end"] == "2026-03-31T12:00:00+00:00"
    row = billing.account_ref("alice").get().to_dict()
    row["subscription_available_milli"] = 100
    billing.account_ref("alice").set(row)
    monkeypatch.setattr(billing, "utcnow", lambda: datetime(2026, 6, 30, 12, tzinfo=timezone.utc))
    assert billing.account("alice")["subscription_minutes"] == 360
    assert billing.account("alice")["subscription_minutes"] == 360
    grants = [r for r in env[0].rows.values() if r.get("kind") == "subscription_allowance"]
    assert len(grants) == 2


def test_funding_order_and_expired_refund(env):
    sub = paid()
    balance = {"free_granted": True, "subscription_available_milli": 2000, "free_available_milli": 1000,
               "legacy_available_milli": 3000, "available_milli": 5000, "reserved_milli": 0,
               "allowance_key": f"sub_alice:{NOW.isoformat()}", "allowance_end": NOW + timedelta(days=30)}
    updated, funding, access = billing.reserve(balance, sub, 8000, 8)
    assert [funding[k] for k in billing.BALANCES] == [2000, 1000, 3000, 2000]
    assert access["max_minutes"] == 180
    game = {"reserved_milli": 8000, "funding": funding}
    returned = billing.release(updated, game, 2500, NOW)
    assert returned["subscription_available_milli"] == 0
    assert returned["free_available_milli"] == 500
    assert returned["legacy_available_milli"] == 3000
    assert returned["available_milli"] == 5000
    next_period = {**updated, "allowance_key": "new-window", "subscription_available_milli": 360000}
    returned = billing.release(next_period, game, 0, NOW + timedelta(days=31))
    assert returned["subscription_available_milli"] == 360000
    assert returned["reserved_milli"] == 0


def test_failed_renewal_grace_and_no_grant(env, monkeypatch):
    billing.subscription_ref("alice").set({**paid(end=NOW), "status": "past_due", "grace_until": NOW + timedelta(days=3)})
    billing.account_ref("alice").set({"free_granted": True, "subscription_available_milli": 4000,
                                     "allowance_end": NOW, "allowance_key": "old"}, merge=True)
    assert billing.account("alice")["plan"] == "pro"
    assert billing.account("alice")["subscription_minutes"] == 4
    monkeypatch.setattr(billing, "utcnow", lambda: NOW + timedelta(days=3))
    assert billing.account("alice")["plan"] == "free"
    assert billing.account("alice")["subscription_minutes"] == 0


def test_concurrent_reservations_cannot_overdraw(env):
    from concurrent.futures import ThreadPoolExecutor
    billing.account_ref("alice").set({"free_granted": True, "free_available_milli": 15000}, merge=True)
    second = "b7c0c3aa-6af9-4d7c-8c54-2523fd65025b"
    for game_id in (RUN, second):
        refs.run_ref("alice", game_id).set({**env[1].get().to_dict(), "status": "uploaded", "declared_duration_seconds": 600})
    def start(game_id):
        try:
            main.start_run(game_id, "alice")
            return 202
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(start, (RUN, second))) == [202, 402]
    assert billing.account("alice")["free_minutes"] == 5


def test_duration_and_topup_restrictions_via_api(env):
    with TestClient(main.app) as api:
        plans = api.get("/api/billing/plans").json()["plans"]
        assert [(p["monthly"], p["annual"], p["minutes"]) for p in plans] == [(0, 0, 15), (29.99, 299.90, 360), (69.99, 699.90, 900)]
        response = api.post("/api/runs/upload", json={"name": "game", "file_name": "game.mp4", "size_bytes": 100, "duration_seconds": 3600})
        assert response.status_code == 402
        assert api.post("/api/billing/checkout", json={"pack_key": "90"}).status_code == 402
        assert api.get("/api/billing/packs").json() == []
        assert api.get(f"/api/runs/{RUN}/csv").status_code == 402
        main.app.dependency_overrides[main.current_user] = lambda: "bob"
        assert api.get(f"/api/runs/{RUN}/csv").status_code == 404


def test_csv_snapshot_survives_downgrade_and_edits(env):
    billing.subscription_ref("alice").set(paid())
    original = main.download_csv(RUN, "alice").body
    billing.subscription_ref("alice").set({**paid(), "status": "canceled"})
    refs.events_ref("alice", RUN).document("e1").update({"included": False})
    assert main.download_csv(RUN, "alice").body == original


def test_reel_success_cache_failure_and_lineage_limits(env, monkeypatch, tmp_path):
    from test_media import fake_storage
    fake_storage(monkeypatch)
    monkeypatch.setattr(reels, "render_reel", lambda *args, **kwargs: {"count": 1, "format": "vertical"})
    queued = reels.queue("alice", RUN, "vertical", [])
    with pytest.raises(HTTPException) as error:
        reels.queue("alice", RUN, "vertical", [])
    assert error.value.status_code == 409
    reels.run("alice", RUN, queued["id"])
    assert billing.usage_ref("alice", RUN).get().to_dict()["successful"] == 1
    cached = reels.queue("alice", RUN, "vertical", [])
    assert cached["status"] == "ready"
    with pytest.raises(HTTPException) as error:
        reels.queue("alice", RUN, "landscape", [])
    assert error.value.status_code == 402
    sibling = "b7c0c3aa-6af9-4d7c-8c54-2523fd65025b"
    refs.run_ref("alice", sibling).set({**env[1].get().to_dict(), "lineage_id": RUN, "latest_reel": None, "reel_job": None})
    refs.events_ref("alice", sibling).document("e1").set(refs.events_ref("alice", RUN).document("e1").get().to_dict())
    with pytest.raises(HTTPException) as error:
        reels.queue("alice", sibling, "vertical", [])
    assert error.value.status_code == 402
    billing.subscription_ref("alice").set(paid())
    queued = reels.queue("alice", sibling, "vertical", [])
    monkeypatch.setattr(reels, "render_reel", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("render failed")))
    with pytest.raises(RuntimeError):
        reels.run("alice", sibling, queued["id"])
    usage = billing.usage_ref("alice", RUN).get().to_dict()
    assert usage["successful"] == 1 and usage["pending"] is None


def test_queued_export_survives_cancel_and_sponsors_gate(env, monkeypatch):
    from test_media import fake_storage
    fake_storage(monkeypatch)
    billing.subscription_ref("alice").set(paid("business"))
    env[1].update({"sponsors": [{"id": "logo", "key": "logo.png", "name": "Sponsor"}]})
    result = reels.queue("alice", RUN, "vertical", ["logo"])
    billing.subscription_ref("alice").set({**paid("business"), "status": "canceled"})
    monkeypatch.setattr(reels, "render_reel", lambda *args, **kwargs: {"count": 1, "format": "vertical"})
    reels.run("alice", RUN, result["id"])
    assert env[1].get().to_dict()["reel_job"]["status"] == "ready"
    with pytest.raises(HTTPException) as error:
        reels.queue("alice", RUN, "landscape", ["logo"])
    assert error.value.status_code == 402


def test_worker_settlement_is_idempotent_and_uses_original_funding(env):
    billing.subscription_ref("alice").set(paid())
    balance, sub = billing.load("alice")
    updated, funding, access = billing.reserve(balance, sub, 60000, 60)
    billing.account_ref("alice").set(updated)
    env[1].update({"status": "running", "worker_token": "token", "funding": funding, "reserved_milli": 60000})
    worker.settle("alice", RUN, "token", 60000, Decimal(3600), .5, "cancelled", None)
    assert billing.account("alice")["subscription_minutes"] == 330
    worker.settle("alice", RUN, "token", 60000, Decimal(3600), .5, "cancelled", None)
    assert billing.account("alice")["subscription_minutes"] == 330


def test_migration_idempotence_and_legacy_funding(env, monkeypatch):
    config = replace(billing.settings, subscriptions_enabled=False, billing_migration_mode=True)
    monkeypatch.setattr(billing, "settings", config)
    billing.account_ref("alice").set({"available_milli": 90000, "reserved_milli": 0})
    assert billing.migrate()["changed"] == 2
    assert billing.migrate()["changed"] == 0
    assert billing.migrate(True)["verified"]
    assert billing.account_ref("alice").get().to_dict()["legacy_available_milli"] == 90000
    monkeypatch.setattr(billing, "settings", replace(config, subscriptions_enabled=True, billing_migration_mode=False))
    balance, sub = billing.load("alice")
    updated, funding, _ = billing.reserve(balance, sub, 60000, 60)
    assert funding["legacy_available_milli"] == 45000
    env[1].update({"legacy_access": True})
    assert billing.entitlement(updated, sub, env[1].get().to_dict())["sponsors"]


def test_migration_requires_drained_jobs_and_verified_marker(env, monkeypatch):
    monkeypatch.setattr(billing, "settings", replace(billing.settings, subscriptions_enabled=False, billing_migration_mode=True))
    env[1].update({"status": "running"})
    with pytest.raises(HTTPException) as error:
        billing.migrate()
    assert error.value.status_code == 409
    monkeypatch.setattr(billing, "settings", replace(billing.settings, subscriptions_enabled=True))
    with pytest.raises(HTTPException) as error:
        billing.enabled()
    assert error.value.status_code == 503


class StripeDouble:
    def __init__(self):
        self.sessions, self.subscriptions, self.schedules = {}, {}, {}
        self.calls = []
        self.prices = {}
        for (plan, interval), price_id in billing.price_ids().items():
            self.prices[price_id] = {"id": price_id, "active": True, "livemode": False, "currency": "usd",
                "unit_amount": billing.price_cents(plan, interval),
                "recurring": {"interval": "month" if interval == "monthly" else "year", "interval_count": 1, "usage_type": "licensed"}}
        self.prices["price_topup"] = {"id": "price_topup", "active": True, "livemode": False, "currency": "usd", "unit_amount": 999}
        self.Price = SimpleNamespace(retrieve=lambda pid: self.prices[pid])
        self.Customer = SimpleNamespace(create=lambda **kwargs: {"id": "cus_alice"})
        self.Subscription = SimpleNamespace(modify=self.modify_subscription, retrieve=lambda sid, **kwargs: copy.deepcopy(self.subscriptions[sid]),
            list=lambda **kwargs: {"data": list(self.subscriptions.values())})
        self.SubscriptionSchedule = SimpleNamespace(create=self.create_schedule, modify=self.modify_schedule,
            release=self.release_schedule, retrieve=lambda sid: self.schedules[sid])
        self.checkout = SimpleNamespace(Session=SimpleNamespace(create=self.create_session, retrieve=lambda sid: self.sessions[sid],
            expire=lambda sid: self.sessions[sid].update(status="expired"), list=lambda **kwargs: {"data": list(self.sessions.values())}, list_line_items=lambda sid, **kwargs: {"data": [{"price": {"id": "price_topup"}}]}))
        self.Invoice = SimpleNamespace(create_preview=self.preview_invoice, retrieve=lambda iid: next(s["latest_invoice"] for s in self.subscriptions.values() if s["latest_invoice"]["id"] == iid))
        self.upgrade_pending = False
        self.preview_due = 2000
        self.config = {"active": True, "livemode": False, "features": {"subscription_update": {"enabled": False}, "subscription_cancel": {"enabled": True, "mode": "at_period_end"}}}
        self.billing_portal = SimpleNamespace(Configuration=SimpleNamespace(retrieve=lambda cid: self.config),
            Session=SimpleNamespace(create=lambda **kwargs: {"url": "https://billing.stripe.test/portal"}))
    def preview_invoice(self, **kwargs):
        self.calls.append(("preview", kwargs))
        return {"amount_due": self.preview_due, "currency": "usd"}
    def modify_subscription(self, sid, **kwargs):
        self.calls.append(("modify_subscription", kwargs))
        sub = self.subscriptions[sid]
        item = sub["items"]["data"][0]
        price = kwargs["items"][0]["price"]
        sub["latest_invoice"] = {"id": "in_upgrade", "status": "open" if self.upgrade_pending else "paid",
            "billing_reason": "subscription_update", "created": kwargs["proration_date"],
            "hosted_invoice_url": "https://invoice.stripe.test/upgrade", "amount_due": self.preview_due,
            "lines": {"data": [{"price": {"id": price}, "amount": self.preview_due,
                "period": {"start": kwargs["proration_date"], "end": item["current_period_end"]}}]}}
        sub["pending_update"] = {"expires_at": kwargs["proration_date"] + 23 * 3600} if self.upgrade_pending else None
        if not self.upgrade_pending:
            item["price"]["id"] = price
        return copy.deepcopy(sub)
    def create_session(self, **kwargs):
        self.calls.append(("checkout", kwargs))
        sid = f"cs_{len(self.sessions)}"
        session = {"id": sid, "url": "https://checkout.stripe.test/" + sid, "status": "open", **kwargs}
        self.sessions[sid] = session
        return session
    def create_schedule(self, **kwargs):
        sid = "sched_alice"
        self.schedules[sid] = {"id": sid, "phases": []}
        self.subscriptions[kwargs["from_subscription"]]["schedule"] = sid
        self.calls.append(("create_schedule", kwargs))
        return self.schedules[sid]
    def modify_schedule(self, sid, **kwargs):
        self.schedules[sid].update(kwargs)
        self.calls.append(("modify_schedule", kwargs))
    def release_schedule(self, sid, **kwargs):
        self.calls.append(("release_schedule", kwargs))
        self.subscriptions["sub_alice"]["schedule"] = None
    def subscription(self, paid_invoice=True, status="active", interval="monthly"):
        end = NOW + timedelta(days=365 if interval == "annual" else 30)
        return {"id": "sub_alice", "customer": "cus_alice", "metadata": {"user_id": "alice"}, "status": status,
                "items": {"data": [{"id": "si_alice", "quantity": 1, "price": {"id": "price_pro_y" if interval == "annual" else "price_pro_m"},
                           "current_period_start": int(NOW.timestamp()), "current_period_end": int(end.timestamp())}]},
                "latest_invoice": {"id": "in_first", "created": int(NOW.timestamp()), "status": "paid" if paid_invoice else "open",
                    "lines": {"data": [{"price": {"id": "price_pro_y" if interval == "annual" else "price_pro_m"}, "period": {"start": int(NOW.timestamp()), "end": int(end.timestamp())}}]}}}


@pytest.fixture
def stripe(env, monkeypatch):
    fake = StripeDouble()
    monkeypatch.setattr(billing, "stripe_client", lambda: fake)
    return fake


def event(kind="invoice.paid", eid="evt_first", obj=None):
    return {"id": eid, "type": kind, "livemode": False, "data": {"object": obj or {"subscription": "sub_alice"}}}


@pytest.mark.parametrize("interval", ["monthly", "annual"])
def test_checkout_price_validation_open_session_and_duplicate_subscription(env, stripe, interval):
    first = billing.checkout("alice", "pro", interval)
    assert first == billing.checkout("alice", "pro", interval)
    assert len(stripe.calls) == 1
    stripe.subscriptions["sub_alice"] = stripe.subscription(interval=interval)
    with pytest.raises(HTTPException) as error:
        billing.checkout("alice", "business", interval)
    assert error.value.status_code == 409
    stripe.prices["price_business_m"]["unit_amount"] = 8000
    with pytest.raises(HTTPException) as error:
        billing.checked_price("business", "monthly")
    assert error.value.status_code == 503


def test_paid_events_deduplicated_and_out_of_order_reconcile_canonical_state(env, stripe):
    billing.subscription_ref("alice").set({"customer_id": "cus_alice"})
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    billing.webhook(event())
    assert billing.account("alice")["subscription_minutes"] == 360
    row = billing.account_ref("alice").get().to_dict()
    row["subscription_available_milli"] = 90000
    billing.account_ref("alice").set(row)
    billing.webhook(event())
    billing.webhook(event("invoice.payment_failed", "evt_old_failure"))
    assert billing.account("alice")["subscription_minutes"] == 90
    assert billing.account("alice")["status"] == "active"
    stripe.subscriptions["sub_alice"]["status"] = "canceled"
    billing.webhook(event("customer.subscription.deleted", "evt_deleted", {"id": "sub_alice"}))
    billing.webhook(event("invoice.paid", "evt_old_paid"))
    assert billing.account("alice")["plan"] == "free"


def test_interrupted_subscription_write_rolls_back_and_retry_grants_once(env, stripe, monkeypatch):
    store = env[0]
    billing.subscription_ref("alice").set({"customer_id": "cus_alice"})
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    before = copy.deepcopy(store.rows)
    original_set = AtomicTransaction.set
    account_path = billing.account_ref("alice").path

    def interrupted_set(tx, ref, value, merge=False):
        original_set(tx, ref, value, merge)
        if ref.path == account_path:
            raise RuntimeError("Simulated database interruption")

    monkeypatch.setattr(AtomicTransaction, "set", interrupted_set)
    with TestClient(main.app, raise_server_exceptions=False) as api:
        import stripe as sdk
        monkeypatch.setattr(sdk.Webhook, "construct_event", lambda *args: event())
        assert api.post("/api/stripe/webhook", json={}).status_code == 500
        assert store.rows == before
        assert "stripe_events/evt_first" not in store.rows
        monkeypatch.setattr(AtomicTransaction, "set", original_set)
        assert api.post("/api/stripe/webhook", json={}).status_code == 200
        assert billing.account("alice")["subscription_minutes"] == 360
        balance = billing.account_ref("alice").get().to_dict()
        balance["subscription_available_milli"] = 90000
        billing.account_ref("alice").set(balance)
        assert api.post("/api/stripe/webhook", json={}).status_code == 200
        assert billing.account("alice")["subscription_minutes"] == 90


def test_failed_initial_payment_cannot_grant_allowance(env, stripe):
    billing.subscription_ref("alice").set({"customer_id": "cus_alice"})
    stripe.subscriptions["sub_alice"] = stripe.subscription(False, "incomplete")
    billing.webhook(event("invoice.payment_failed"))
    assert billing.account("alice")["subscription_minutes"] == 0
    assert billing.account("alice")["plan"] == "free"
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    billing.webhook(event("invoice.paid", "evt_later_paid"))
    assert billing.account("alice")["subscription_minutes"] == 360


def test_schedule_upgrade_downgrade_and_interval_change_preserve_current_access(env, stripe):
    billing.subscription_ref("alice").set(paid())
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    result = billing.schedule_change("alice", "business", "annual")
    assert result["scheduled_change"]["effective_at"] == (NOW + timedelta(days=30)).isoformat()
    assert billing.account("alice")["plan"] == "pro"
    phases = stripe.schedules["sched_alice"]["phases"]
    assert phases[0]["items"][0]["price"] == "price_pro_m"
    assert phases[1]["items"][0]["price"] == "price_business_y"
    billing.schedule_change("alice", "pro", "annual")
    assert stripe.schedules["sched_alice"]["phases"][1]["items"][0]["price"] == "price_pro_y"
    assert billing.schedule_change("alice", "pro", "monthly")["scheduled_change"] is None


def test_portal_configuration_and_schedule_release(env, stripe):
    billing.subscription_ref("alice").set({**paid(), "schedule_id": "sched_alice", "scheduled_change": {"plan": "business"}})
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    assert billing.portal("alice")["url"].startswith("https://billing.")
    assert billing.subscription_ref("alice").get().to_dict()["scheduled_change"] is None
    stripe.config["features"]["subscription_update"]["enabled"] = True
    with pytest.raises(HTTPException) as error:
        billing.portal("alice")
    assert error.value.status_code == 503


def test_topup_async_confirmation_and_duplicate_grants(env, stripe):
    billing.subscription_ref("alice").set(paid())
    billing.topup("alice")
    session = stripe.sessions["cs_0"]
    session.update(payment_status="unpaid", currency="usd", amount_total=999)
    billing.webhook(event("checkout.session.completed", obj={"id": "cs_0"}))
    assert billing.account("alice")["prepaid_minutes"] == 0
    session["payment_status"] = "paid"
    billing.webhook(event("checkout.session.async_payment_succeeded", "evt_async", {"id": "cs_0"}))
    billing.webhook(event("checkout.session.completed", "evt_completed", {"id": "cs_0"}))
    assert billing.account("alice")["prepaid_minutes"] == 90


def test_webhook_signature_invalid_live_event_and_durable_failure(env, stripe, monkeypatch):
    import stripe as sdk
    monkeypatch.setattr(sdk.Webhook, "construct_event", lambda *args: (_ for _ in ()).throw(ValueError("bad signature")))
    with TestClient(main.app) as api:
        assert api.post("/api/stripe/webhook", json={}).status_code == 400
        monkeypatch.setattr(sdk.Webhook, "construct_event", lambda *args: {**event(), "livemode": True})
        assert api.post("/api/stripe/webhook", json={}).status_code == 400
        monkeypatch.setattr(sdk.Webhook, "construct_event", lambda *args: event())
        stripe.subscriptions["sub_alice"] = stripe.subscription()
        billing.subscription_ref("alice").set({"customer_id": "wrong"})
        assert api.post("/api/stripe/webhook", json={}).status_code == 400
        assert "stripe_events/evt_first" not in env[0].rows


def test_maintenance_blocks_mutations_but_not_migration(env, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, billing_migration_mode=True))
    with TestClient(main.app) as api:
        assert api.post(f"/api/runs/{RUN}/reel", json={}).status_code == 503
        assert api.get("/api/billing/plans").status_code == 200


def test_subscription_update_before_new_invoice_payment_does_not_grant(env, stripe):
    billing.subscription_ref("alice").set({"customer_id": "cus_alice"})
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    billing.webhook(event())
    row = billing.account_ref("alice").get().to_dict()
    row["subscription_available_milli"] = 1000
    billing.account_ref("alice").set(row)
    item = stripe.subscriptions["sub_alice"]["items"]["data"][0]
    item["current_period_start"] += 30 * 86400
    item["current_period_end"] += 30 * 86400
    billing.webhook(event("customer.subscription.updated", "evt_advanced", {"id": "sub_alice"}))
    assert billing.account("alice")["subscription_minutes"] == 1
    assert billing.subscription_ref("alice").get().to_dict()["paid_until"] == NOW + timedelta(days=30)


def test_lost_checkout_response_recovers_open_session(env, stripe):
    first = billing.checkout("alice", "pro", "monthly")
    billing.subscription_ref("alice").update({"checkout_id": None})
    assert billing.checkout("alice", "pro", "monthly") == first
    assert len(stripe.sessions) == 1


def test_monthly_short_month_does_not_grant_before_paid_renewal(env, monkeypatch):
    start = datetime(2026, 2, 28, 12, tzinfo=timezone.utc)
    end = datetime(2026, 3, 31, 12, tzinfo=timezone.utc)
    billing.subscription_ref("alice").set(paid(start=start, end=end))
    monkeypatch.setattr(billing, "utcnow", lambda: datetime(2026, 3, 1, 12, tzinfo=timezone.utc))
    billing.account("alice")
    billing.account_ref("alice").update({"subscription_available_milli": 1000})
    monkeypatch.setattr(billing, "utcnow", lambda: datetime(2026, 3, 28, 12, tzinfo=timezone.utc))
    assert billing.account("alice")["subscription_minutes"] == 1
    assert billing.account("alice")["allowance_end"] == end.isoformat()


def test_delayed_failed_invoice_restores_only_previous_remaining_minutes_for_grace(env):
    sub = paid(end=NOW)
    billing.subscription_ref("alice").set(sub)
    billing.account_ref("alice").set({"free_granted": True, "subscription_available_milli": 4000,
                                     "allowance_end": NOW, "allowance_key": "old"}, merge=True)
    assert billing.account("alice")["subscription_minutes"] == 0
    billing.subscription_ref("alice").set({**sub, "status": "past_due", "grace_until": NOW + timedelta(days=3)})
    assert billing.account("alice")["subscription_minutes"] == 4
    assert billing.account("alice")["subscription_minutes"] == 4


def test_existing_scheduler_refreshes_annual_accounts(env):
    billing.subscription_ref("alice").set(paid(interval="annual", end=NOW + timedelta(days=365)))
    assert billing.refresh_all() == 1
    assert billing.account("alice")["subscription_minutes"] == 360


def test_expired_cancel_refunds_only_permanent_funding_and_ledger_matches(env, monkeypatch):
    billing.subscription_ref("alice").set(paid())
    billing.account_ref("alice").set({"free_granted": True, "subscription_available_milli": 0,
        "available_milli": 0, "reserved_milli": 6000, "allowance_key": "old", "allowance_end": NOW}, merge=True)
    env[1].update({"status": "queued", "reserved_milli": 6000,
                  "funding": {"subscription_available_milli": 5000, "available_milli": 1000, "allowance_key": "old"}})
    main.cancel_run(RUN, "alice")
    balance = billing.account_ref("alice").get().to_dict()
    assert balance["available_milli"] == 1000 and balance["subscription_available_milli"] == 0
    assert balance["reserved_milli"] == 0
    assert refs.ledger_ref("alice", f"run:{RUN}:cancel").get().to_dict()["available_delta_milli"] == 1000


def test_sponsor_upload_api_rejects_free_and_pro(env):
    with TestClient(main.app) as api:
        assert api.post(f"/api/runs/{RUN}/sponsors", files={"file": ("logo.png", b"synthetic", "image/png")}).status_code == 402
        billing.subscription_ref("alice").set(paid())
        assert api.post(f"/api/runs/{RUN}/sponsors", files={"file": ("logo.png", b"synthetic", "image/png")}).status_code == 402


def test_grant_receipt_records_full_allowance_even_when_reserved_immediately(env):
    billing.subscription_ref("alice").set(paid())
    env[1].update({"status": "uploaded", "declared_duration_seconds": 3600})
    main.start_run(RUN, "alice")
    grants = [row for row in env[0].rows.values() if row.get("kind") == "subscription_allowance"]
    assert len(grants) == 1 and grants[0]["amount_milli"] == 360000
    assert billing.account("alice")["subscription_minutes"] == 300


def test_real_sdk_resources_are_normalized_for_prices_subscriptions_portal_and_webhooks(env, stripe, monkeypatch):
    import stripe as sdk
    stripe.Price.retrieve = lambda pid: sdk.Price.construct_from(stripe.prices[pid], None)
    assert billing.checked_price("pro", "monthly") == "price_pro_m"
    billing.subscription_ref("alice").set({"customer_id": "cus_alice"})
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    stripe.Subscription.retrieve = lambda sid, **kwargs: sdk.Subscription.construct_from(copy.deepcopy(stripe.subscriptions[sid]), None)
    stripe.Subscription.list = lambda **kwargs: sdk.ListObject.construct_from({"object": "list", "data": list(stripe.subscriptions.values())}, None)
    stripe.billing_portal.Configuration.retrieve = lambda cid: sdk.billing_portal.Configuration.construct_from(stripe.config, None)
    stripe.billing_portal.Session.create = lambda **kwargs: sdk.billing_portal.Session.construct_from({"url": "https://billing.stripe.test/portal"}, None)
    resource_event = sdk.Event.construct_from(event(), None)
    billing.webhook(resource_event)
    assert billing.account("alice")["subscription_minutes"] == 360
    assert billing.portal("alice")["url"].startswith("https://billing.")
    monkeypatch.setattr(sdk.Webhook, "construct_event", lambda *args: resource_event)
    with TestClient(main.app) as api:
        assert api.post("/api/stripe/webhook", json={}).status_code == 200


@pytest.mark.parametrize("plan,minutes,old_minutes", [("pro", 360, 180), ("business", 900, 480)])
@pytest.mark.parametrize("interval", ["monthly", "annual"])
def test_updated_allowances_new_and_existing_windows(env, monkeypatch, plan, minutes, old_minutes, interval):
    end = billing.add_months(NOW, 12 if interval == "annual" else 1)
    sub = paid(plan=plan, interval=interval, end=end)
    billing.subscription_ref("alice").set(sub)
    assert billing.account("alice")["subscription_minutes"] == minutes
    assert main.pricing_plans()["plans"][["free", "pro", "business"].index(plan)]["minutes"] == minutes
    # Simulate a partially consumed window granted under the previous definitions.
    billing.account_ref("alice").set({"subscription_available_milli": (old_minutes - 60) * 1000,
        "allowance_grant_milli": old_minutes * 1000, "available_milli": 60000,
        "reserved_milli": 10000}, merge=True)
    for _ in range(2):
        current = billing.account("alice")
        assert current["subscription_minutes"] == old_minutes - 60
        assert current["prepaid_minutes"] == 60
        assert current["reserved_minutes"] == 10
    boundary = billing.add_months(NOW, 1)
    if interval == "monthly":
        billing.subscription_ref("alice").set(paid(plan=plan, interval=interval, start=boundary,
            end=billing.add_months(NOW, 2)))
    monkeypatch.setattr(billing, "utcnow", lambda: boundary)
    for _ in range(2):
        renewed = billing.account("alice")
        assert renewed["subscription_minutes"] == minutes
        assert renewed["prepaid_minutes"] == 60
        assert renewed["reserved_minutes"] == 10
    grants = [row for row in env[0].rows.values() if row.get("kind") == "subscription_allowance"]
    assert len(grants) == 2
    assert all(row["amount_milli"] == minutes * 1000 for row in grants)
    assert renewed["allowance_end"] == billing.add_months(NOW, 2).isoformat()


@pytest.mark.parametrize("status", ["running", "cancel_requested"])
def test_worker_heartbeat_preserves_progress_and_cancellation(env, status):
    _, game = env
    progress = {"phase": "analyzing", "completed": 6720, "total": 18397}
    game.update({"status": status, "worker_token": "owner", "progress": progress,
                 "lease_expires_at": NOW})
    assert worker.renew_lease("alice", RUN, "owner")
    data = game.get().to_dict()
    assert data["status"] == status
    assert data["progress"] == progress
    assert data["lease_expires_at"] == NOW + timedelta(minutes=10)
    assert not worker.renew_lease("alice", RUN, "old-owner")


def test_worker_claim_clears_retry_warning(env):
    _, game = env
    game.update({"status": "queued", "worker_token": None,
                 "error": "Worker lease expired; retrying"})
    claimed = worker.claim_run("alice", RUN, "replacement")
    assert claimed["error"] is None
    assert game.get().to_dict()["error"] is None
    assert worker.claim_run("alice", RUN, "duplicate") is None


def test_analysis_dispatch_has_long_game_timeout(monkeypatch):
    from google.cloud import run_v2
    from hosted.app import tasks
    calls = []
    runner = SimpleNamespace(job_path=lambda *args: "job", run_job=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(run_v2, "JobsClient", lambda: runner)
    monkeypatch.setattr(tasks, "settings", SimpleNamespace(gcp_project_id="project", gcp_run_job="worker", gcp_region="region"))
    tasks.launch_job(RUN, "alice")
    assert calls[0]["request"]["overrides"]["timeout"]["seconds"] == 21600


@pytest.mark.parametrize("plan,interval,cents", [("pro", "monthly", 2999), ("pro", "annual", 29990), ("business", "monthly", 6999), ("business", "annual", 69990)])
def test_decimal_catalog_uses_exact_integer_cents(env, stripe, plan, interval, cents):
    assert billing.price_cents(plan, interval) == cents
    assert isinstance(billing.price_cents(plan, interval), int)
    billing.checked_price(plan, interval)
    stripe.prices[billing.price_ids()[plan, interval]]["unit_amount"] = cents + 1
    with pytest.raises(HTTPException):
        billing.checked_price(plan, interval)


def test_topup_catalog_and_obsolete_pack(env):
    billing.subscription_ref("alice").set(paid())
    with TestClient(main.app) as api:
        assert api.get("/api/billing/packs").json() == [{"id": "90", "minutes": 90, "price_usd": 9.99}]
        assert api.post("/api/billing/checkout", json={"pack_key": "60"}).status_code == 400


@pytest.mark.parametrize("interval", ["monthly", "annual"])
def test_paid_immediate_upgrade_preserves_spend_reservations_and_window(env, stripe, monkeypatch, interval):
    billing.subscription_ref("alice").set(paid(interval=interval, end=NOW + timedelta(days=365 if interval == "annual" else 30)))
    stripe.subscriptions["sub_alice"] = stripe.subscription(interval=interval)
    original = billing.load("alice")[0]
    billing.account_ref("alice").set({"subscription_available_milli": 100000, "reserved_milli": 20000, "available_milli": 5000}, merge=True)
    monkeypatch.setattr(billing, "utcnow", lambda: NOW + timedelta(days=15))
    quote = billing.upgrade_preview("alice", "business", interval)
    assert quote["amount_due"] == 2000
    expected_extra = 270000 if interval == "monthly" else 540000 * 16 // 31
    assert round(quote["extra_minutes"] * 1000) == expected_extra
    result = billing.schedule_change("alice", "business", interval, quote["quote_id"])
    assert result["status"] == "upgraded"
    updated = billing.load("alice")[0]
    assert updated["allowance_key"] == original["allowance_key"]
    assert updated["allowance_end"] == original["allowance_end"]
    assert updated["subscription_available_milli"] == 100000 + expected_extra
    assert updated["reserved_milli"] == 20000 and updated["available_milli"] == 5000
    assert billing.account("alice")["entitlements"]["sponsors"] is True
    modification = next(params for name, params in stripe.calls if name == "modify_subscription")
    assert modification["payment_behavior"] == "pending_if_incomplete"
    assert modification["proration_behavior"] == "always_invoice"
    assert "billing_cycle_anchor" not in modification
    # Retrying the same confirmation and reordered events cannot charge or grant twice.
    billing.schedule_change("alice", "business", interval, quote["quote_id"])
    billing.webhook(event("customer.subscription.updated", "evt_after_upgrade", {"id": "sub_alice"}))
    billing.webhook(event("invoice.paid", "evt_duplicate_upgrade_invoice"))
    assert billing.load("alice")[0] == updated
    assert sum(name == "modify_subscription" for name, _ in stripe.calls) == 1
    receipts = [row for row in env[0].rows.values() if row.get("kind") == "subscription_upgrade"]
    assert len(receipts) == 1 and receipts[0]["available_delta_milli"] == expected_extra


def test_pending_upgrade_preserves_pro_and_later_paid_invoice_grants_once(env, stripe, monkeypatch):
    billing.subscription_ref("alice").set(paid())
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    monkeypatch.setattr(billing, "utcnow", lambda: NOW + timedelta(days=15))
    billing.load("alice")
    billing.account_ref("alice").set({"subscription_available_milli": 10000}, merge=True)
    stripe.upgrade_pending = True
    quote = billing.upgrade_preview("alice", "business", "monthly")
    result = billing.schedule_change("alice", "business", "monthly", quote["quote_id"])
    assert result["status"] == "payment_required"
    assert result["payment_url"] == "https://invoice.stripe.test/upgrade"
    assert billing.account("alice")["plan"] == "pro"
    assert billing.account("alice")["subscription_minutes"] == 10
    assert billing.account("alice")["pending_upgrade"]["payment_url"] == result["payment_url"]
    billing.schedule_change("alice", "business", "monthly", quote["quote_id"])
    assert sum(name == "modify_subscription" for name, _ in stripe.calls) == 1
    with pytest.raises(HTTPException):
        billing.upgrade_preview("alice", "business", "monthly")
    sub = stripe.subscriptions["sub_alice"]
    sub["pending_update"] = None
    sub["items"]["data"][0]["price"]["id"] = "price_business_m"
    sub["latest_invoice"]["status"] = "paid"
    billing.webhook(event("invoice.paid", "evt_upgrade_paid"))
    assert billing.account("alice")["plan"] == "business"
    assert billing.account("alice")["subscription_minutes"] == 280
    assert billing.account("alice")["pending_upgrade"] is None
    billing.webhook(event("invoice.payment_failed", "evt_old_upgrade_failure"))
    assert billing.account("alice")["subscription_minutes"] == 280


def test_upgrade_quote_expiry_amount_change_and_wrong_quote_cannot_charge(env, stripe, monkeypatch):
    billing.subscription_ref("alice").set(paid())
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    quote = billing.upgrade_preview("alice", "business", "monthly")
    for quote_id in ["another-account-quote"]:
        with pytest.raises(HTTPException):
            billing.schedule_change("alice", "business", "monthly", quote_id)
    stripe.preview_due += 1
    with pytest.raises(HTTPException):
        billing.schedule_change("alice", "business", "monthly", quote["quote_id"])
    stripe.preview_due -= 1
    monkeypatch.setattr(billing, "utcnow", lambda: NOW + timedelta(minutes=6))
    with pytest.raises(HTTPException):
        billing.schedule_change("alice", "business", "monthly", quote["quote_id"])
    assert not any(name == "modify_subscription" for name, _ in stripe.calls)


def test_immediate_upgrade_requires_confirmation_and_clears_prior_schedule(env, stripe):
    billing.subscription_ref("alice").set(paid())
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    with pytest.raises(HTTPException):
        billing.schedule_change("alice", "business", "monthly")
    billing.schedule_change("alice", "business", "annual")
    quote = billing.upgrade_preview("alice", "business", "monthly")
    billing.schedule_change("alice", "business", "monthly", quote["quote_id"])
    assert stripe.subscriptions["sub_alice"]["schedule"] is None
    assert billing.account("alice")["scheduled_change"] is None


def test_annual_upgrade_next_month_gets_full_allowance_and_active_reservation_refunds(env, stripe, monkeypatch):
    billing.subscription_ref("alice").set(paid(interval="annual", end=NOW + timedelta(days=365)))
    stripe.subscriptions["sub_alice"] = stripe.subscription(interval="annual")
    balance, sub = billing.load("alice")
    reserved, funding, _ = billing.reserve(balance, sub, 20000, 20)
    billing.account_ref("alice").set(reserved)
    monkeypatch.setattr(billing, "utcnow", lambda: NOW + timedelta(days=15))
    quote = billing.upgrade_preview("alice", "business", "annual")
    billing.schedule_change("alice", "business", "annual", quote["quote_id"])
    upgraded = billing.load("alice")[0]
    released = billing.release(upgraded, {"reserved_milli": 20000, "funding": funding}, 0, billing.utcnow())
    assert released["subscription_available_milli"] == upgraded["subscription_available_milli"] + 20000
    monkeypatch.setattr(billing, "utcnow", lambda: billing.add_months(NOW, 1))
    renewed = billing.load("alice")[0]
    assert renewed["subscription_available_milli"] == 900000


def test_late_upgrade_payment_never_grants_expired_extra_minutes(env, stripe, monkeypatch):
    billing.subscription_ref("alice").set(paid(interval="annual", end=NOW + timedelta(days=365)))
    stripe.subscriptions["sub_alice"] = stripe.subscription(interval="annual")
    monkeypatch.setattr(billing, "utcnow", lambda: billing.add_months(NOW, 1) - timedelta(minutes=1))
    stripe.upgrade_pending = True
    quote = billing.upgrade_preview("alice", "business", "annual")
    billing.schedule_change("alice", "business", "annual", quote["quote_id"])
    sub = stripe.subscriptions["sub_alice"]
    sub["pending_update"] = None
    sub["items"]["data"][0]["price"]["id"] = "price_business_y"
    sub["latest_invoice"]["status"] = "paid"
    monkeypatch.setattr(billing, "utcnow", lambda: billing.add_months(NOW, 1) + timedelta(minutes=1))
    billing.webhook(event("invoice.paid", "evt_late_upgrade"))
    assert billing.account("alice")["subscription_minutes"] == 900
    receipt = next(row for row in env[0].rows.values() if row.get("kind") == "subscription_upgrade")
    assert receipt["available_delta_milli"] == 0


def test_upgrade_retry_after_lost_stripe_response_uses_same_invoice_key(env, stripe, monkeypatch):
    billing.subscription_ref("alice").set(paid())
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    quote = billing.upgrade_preview("alice", "business", "monthly")
    modify = stripe.Subscription.modify
    def interrupted(*args, **kwargs):
        modify(*args, **kwargs)
        raise RuntimeError("Response interrupted after Stripe applied the payment")
    monkeypatch.setattr(stripe.Subscription, "modify", interrupted)
    with pytest.raises(RuntimeError):
        billing.upgrade_now("alice", "business", "monthly", quote["quote_id"])
    monkeypatch.setattr(stripe.Subscription, "modify", modify)
    assert billing.upgrade_now("alice", "business", "monthly", quote["quote_id"])["status"] == "upgraded"
    assert sum(name == "modify_subscription" for name, _ in stripe.calls) == 1
    assert billing.account("alice")["subscription_minutes"] == 900


def test_expired_pending_upgrade_cannot_reuse_old_quote(env, stripe, monkeypatch):
    billing.subscription_ref("alice").set(paid())
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    stripe.upgrade_pending = True
    quote = billing.upgrade_preview("alice", "business", "monthly")
    billing.upgrade_now("alice", "business", "monthly", quote["quote_id"])
    stripe.subscriptions["sub_alice"]["pending_update"] = None
    stripe.subscriptions["sub_alice"]["latest_invoice"]["status"] = "void"
    monkeypatch.setattr(billing, "utcnow", lambda: NOW + timedelta(days=1))
    with pytest.raises(HTTPException):
        billing.upgrade_now("alice", "business", "monthly", quote["quote_id"])
    assert billing.account("alice")["pending_upgrade"] is None
    assert sum(name == "modify_subscription" for name, _ in stripe.calls) == 1


def test_upgrade_endpoints_require_quote_for_charge_and_use_verified_identity(env, stripe):
    billing.subscription_ref("alice").set(paid())
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    with TestClient(main.app) as api:
        assert api.post("/api/billing/subscription/change", json={"plan": "business", "interval": "monthly"}).status_code == 409
        quote = api.post("/api/billing/subscription/preview", json={"plan": "business", "interval": "monthly"})
        assert quote.status_code == 200
        main.app.dependency_overrides[main.current_user] = lambda: "bob"
        response = api.post("/api/billing/subscription/change", json={"plan": "business", "interval": "monthly", "quote_id": quote.json()["quote_id"]})
        assert response.status_code == 409
        assert not any(name == "modify_subscription" for name, _ in stripe.calls)
        main.app.dependency_overrides[main.current_user] = lambda: "alice"
        response = api.post("/api/billing/subscription/change", json={"plan": "business", "interval": "monthly", "quote_id": quote.json()["quote_id"]})
        assert response.status_code == 200 and response.json()["status"] == "upgraded"


def test_upgrade_projection_interruption_rolls_back_grant_and_retries_without_new_charge(env, stripe, monkeypatch):
    billing.subscription_ref("alice").set(paid())
    stripe.subscriptions["sub_alice"] = stripe.subscription()
    quote = billing.upgrade_preview("alice", "business", "monthly")
    original_set = AtomicTransaction.set
    interrupted = [False]
    def fail_grant(tx, ref, data, **kwargs):
        original_set(tx, ref, data, **kwargs)
        if data.get("kind") == "subscription_upgrade" and not interrupted[0]:
            interrupted[0] = True
            raise RuntimeError("Interrupted upgrade projection")
    monkeypatch.setattr(AtomicTransaction, "set", fail_grant)
    with pytest.raises(RuntimeError):
        billing.upgrade_now("alice", "business", "monthly", quote["quote_id"])
    assert billing.account("alice")["plan"] == "pro"
    assert not any(row.get("kind") == "subscription_upgrade" for row in env[0].rows.values())
    billing.upgrade_now("alice", "business", "monthly", quote["quote_id"])
    assert billing.account("alice")["subscription_minutes"] == 900
    assert sum(name == "modify_subscription" for name, _ in stripe.calls) == 1


def test_upgrade_confirmation_cannot_cross_allowance_deadline(env, stripe, monkeypatch):
    billing.subscription_ref("alice").set(paid(interval="annual", end=NOW + timedelta(days=365)))
    stripe.subscriptions["sub_alice"] = stripe.subscription(interval="annual")
    boundary = billing.add_months(NOW, 1)
    monkeypatch.setattr(billing, "utcnow", lambda: boundary - timedelta(minutes=1))
    quote = billing.upgrade_preview("alice", "business", "annual")
    monkeypatch.setattr(billing, "utcnow", lambda: boundary + timedelta(seconds=1))
    with pytest.raises(HTTPException):
        billing.upgrade_now("alice", "business", "annual", quote["quote_id"])
    assert not any(name == "modify_subscription" for name, _ in stripe.calls)
