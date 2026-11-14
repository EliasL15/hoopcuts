"""Subscription projection, allowance accounting and Stripe orchestration.

Stripe owns payments. Firestore transactions own grants, reservations and access.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from fastapi import HTTPException

from .config import settings
from .db import account_ref, client, ledger_ref, run_ref, utcnow

log = logging.getLogger(__name__)
PLANS = {
    "free": {"name": "Free", "monthly": 0, "annual": 0, "minutes": 15, "renders": 1, "csv": False, "sponsors": False},
    "pro": {"name": "Pro", "monthly": 29.99, "annual": 299.90, "minutes": 360, "renders": 3, "csv": True, "sponsors": False},
    "business": {"name": "Business", "monthly": 69.99, "annual": 699.90, "minutes": 900, "renders": 6, "csv": True, "sponsors": True},
}
TOPUP = {"id": "90", "minutes": 90, "price_usd": 9.99, "amount_cents": 999}


def price_cents(plan, interval):
    return int(Decimal(str(PLANS[plan][interval])) * 100)


ACTIVE = {"active", "past_due"}
TERMINAL = {"canceled", "incomplete_expired"}
BALANCES = ("subscription_available_milli", "free_available_milli", "legacy_available_milli", "available_milli")


def stripe_live_mode():
    if settings.stripe_mode not in {"test", "live"}:
        raise HTTPException(503, "STRIPE_MODE must be test or live")
    return settings.stripe_mode == "live"


def require_live_transition():
    if stripe_live_mode():
        marker = client().collection("maintenance").document("stripe-live-v1").get().to_dict() or {}
        if not marker.get("verified"):
            raise HTTPException(503, "Live billing account transition must be verified before activation")


def require_event_mode(event):
    if bool(event.get("livemode")) != stripe_live_mode():
        raise HTTPException(400, "Stripe event does not match the configured billing mode")


def require_balance_mode(balance):
    mode = balance.get("stripe_mode")
    if (mode and mode != settings.stripe_mode) or (stripe_live_mode() and balance and mode != "live"):
        raise HTTPException(503, "This account must complete the billing environment transition")


def subscription_ref(uid):
    return client().collection("users").document(uid).collection("account").document("subscription-live" if stripe_live_mode() else "subscription")


def usage_ref(uid, root):
    return client().collection("users").document(uid).collection("reel_usage").document(root)


def enabled():
    if not settings.subscriptions_enabled:
        if stripe_live_mode():
            raise HTTPException(503, "Live billing requires subscriptions to remain enabled")
        return False
    require_live_transition()
    if stripe_live_mode() and not settings.stripe_live_enabled:
        raise HTTPException(503, "Live billing is prepared but not activated")
    marker = client().collection("maintenance").document("subscriptions-v1").get().to_dict() or {}
    if not marker.get("verified"):
        raise HTTPException(503, "Subscription migration must be verified before enabling billing")
    return True


def add_months(anchor, count):
    month = anchor.year * 12 + anchor.month - 1 + count
    year, index = divmod(month, 12)
    return anchor.replace(year=year, month=index + 1,
                          day=min(anchor.day, calendar.monthrange(year, index + 1)[1]))


def allowance_window(anchor, now, paid_until):
    count = max(0, (now.year - anchor.year) * 12 + now.month - anchor.month)
    if add_months(anchor, count) > now:
        count = max(0, count - 1)
    start = add_months(anchor, count)
    return start, min(add_months(anchor, count + 1), paid_until)


def paid_access(sub, now):
    if sub.get("status") not in ACTIVE or sub.get("pause_collection"):
        return False
    paid_until = sub.get("paid_until")
    if not paid_until:
        return False
    if sub["status"] == "past_due":
        grace = sub.get("grace_until")
        return bool(grace and now < grace)
    return now < paid_until


def refreshed(balance, sub, now):
    require_balance_mode(balance)
    result = dict(balance)
    if stripe_live_mode():
        result["stripe_mode"] = "live"
    if not result.get("free_granted"):
        result.update(free_granted=True, free_available_milli=15000)
    result.setdefault("available_milli", 0)
    result.setdefault("reserved_milli", 0)
    access = paid_access(sub, now)
    # Retain expired remaining minutes separately so a delayed failure webhook can
    # restore them for grace without ever making them usable on a Free account.
    if access and result.get("expired_allowance_key") == result.get("allowance_key"):
        result["subscription_available_milli"] = int(result.get("subscription_available_milli", 0)) + int(result.get("expired_subscription_milli", 0))
        result["expired_subscription_milli"] = 0
    if access and sub.get("status") == "active":
        if sub.get("interval") == "monthly":
            start, end = sub["allowance_anchor"], sub["paid_until"]
        else:
            start, end = allowance_window(sub["allowance_anchor"], now, sub["paid_until"])
        window = f"{sub['subscription_id']}:{start.isoformat()}"
        if result.get("allowance_key") != window:
            result.update(allowance_key=window, subscription_available_milli=PLANS[sub.get("paid_plan", sub["plan"])]["minutes"] * 1000,
                          allowance_start=start, allowance_end=end, allowance_grant_milli=PLANS[sub.get("paid_plan", sub["plan"])]["minutes"] * 1000, expired_subscription_milli=0, expired_allowance_key=None)
    elif not access:
        if int(result.get("subscription_available_milli", 0)):
            result.update(expired_subscription_milli=result["subscription_available_milli"],
                          expired_allowance_key=result.get("allowance_key"))
        result["subscription_available_milli"] = 0
    return result


def load(uid):
    from google.cloud import firestore
    ref, subref = account_ref(uid), subscription_ref(uid)
    @firestore.transactional
    def refresh(tx):
        balance = next(tx.get(ref)).to_dict() or {}
        sub = next(tx.get(subref)).to_dict() or {}
        updated = refreshed(balance, sub, utcnow())
        record_allowance(tx, uid, balance, updated)
        if updated != balance:
            tx.set(ref, updated, merge=True)
        return updated, sub
    return refresh(client().transaction())


def entitlement(balance, sub, game=None):
    now = utcnow()
    plan = sub.get("paid_plan", sub.get("plan", "free")) if paid_access(sub, now) else "free"
    legacy = bool((game or {}).get("legacy_access"))
    return {"plan": plan, "csv": legacy or PLANS[plan]["csv"], "sponsors": legacy or PLANS[plan]["sponsors"],
            "renders": None if legacy else PLANS[plan]["renders"],
            "max_minutes": 180 if plan != "free" or int(balance.get("legacy_available_milli", 0)) > 0 else 15}


def account(uid):
    balance, sub = load(uid)
    access = entitlement(balance, sub)
    return {"available_minutes": sum(int(balance.get(k, 0)) for k in BALANCES) / 1000,
            "reserved_minutes": int(balance.get("reserved_milli", 0)) / 1000,
            "subscription_minutes": int(balance.get("subscription_available_milli", 0)) / 1000,
            "free_minutes": int(balance.get("free_available_milli", 0)) / 1000,
            "prepaid_minutes": int(balance.get("available_milli", 0)) / 1000,
            "legacy_minutes": int(balance.get("legacy_available_milli", 0)) / 1000,
            "plan": access["plan"], "subscription_plan": sub.get("plan"), "status": sub.get("status", "free"),
            "interval": sub.get("interval"), "renewal_at": iso(sub.get("current_period_end")),
            "allowance_end": iso(balance.get("allowance_end")), "grace_until": iso(sub.get("grace_until")),
            "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
            "pending_upgrade": sub.get("pending_upgrade") if (sub.get("pending_upgrade") or {}).get("expires_at", "") > iso(utcnow()) else None,
            "scheduled_change": sub.get("scheduled_change"), "can_top_up": access["plan"] != "free",
            "can_manage": bool(sub.get("customer_id")), "has_subscription": sub.get("status") not in TERMINAL | {None},
            "entitlements": access, "subscriptions_enabled": True}


def iso(value):
    return value.isoformat() if value else None


def require_minutes(balance, amount, buckets=BALANCES):
    available = sum(int(balance.get(key, 0)) for key in buckets)
    if available < amount:
        raise HTTPException(402, f"Not enough credits. This video needs {amount / 1000:g} video minutes; "
                            f"you have {available / 1000:g} available. Add minutes or choose a shorter video.")


def reserve(balance, sub, amount, duration_minutes):
    updated = refreshed(balance, sub, utcnow())
    access = entitlement(updated, sub)
    if duration_minutes > access["max_minutes"]:
        raise HTTPException(402, "Free uploads are limited to 15 minutes. Upgrade to analyze a full game.")
    require_minutes(updated, amount)
    # Subscription first; free trial next; legacy credits precede new prepaid.
    funding, remaining = {}, amount
    for key in BALANCES:
        used = min(remaining, int(updated.get(key, 0)))
        funding[key] = used
        updated[key] = int(updated.get(key, 0)) - used
        remaining -= used
    updated["reserved_milli"] = int(updated.get("reserved_milli", 0)) + amount
    funding["allowance_key"] = updated.get("allowance_key")
    return updated, funding, access


def reserve_prepaid(balance, amount):
    updated = dict(balance)
    remaining, funding = amount, {}
    require_minutes(updated, amount, ("legacy_available_milli", "available_milli"))
    for key in ("legacy_available_milli", "available_milli"):
        used = min(remaining, int(updated.get(key, 0)))
        updated[key] = int(updated.get(key, 0)) - used
        funding[key] = used
        remaining -= used
    updated["reserved_milli"] = int(updated.get("reserved_milli", 0)) + amount
    return updated, funding


def release(balance, game, charged, now):
    """Refund unused funding to its original bucket, dropping expired allowance."""
    updated = dict(balance)
    funding = game.get("funding")
    if not funding:
        # Jobs accepted before migration still settle exactly their old reservation.
        key = "legacy_available_milli" if game.get("billing_migrated") else "available_milli"
        funding = {key: int(game.get("reserved_milli", 0))}
    remaining_charge = charged
    for key in BALANCES:
        held = int(funding.get(key, 0))
        consumed = min(held, remaining_charge)
        remaining_charge -= consumed
        refund = held - consumed
        if key == "subscription_available_milli" and (
                funding.get("allowance_key") != balance.get("allowance_key") or
                not balance.get("allowance_end") or now >= balance["allowance_end"]):
            refund = 0
        if held or key in updated:
            updated[key] = int(updated.get(key, 0)) + refund
    updated["reserved_milli"] = max(0, int(balance.get("reserved_milli", 0)) - int(game.get("reserved_milli", 0)))
    return updated


def price_ids():
    return {("pro", "monthly"): settings.stripe_price_pro_monthly,
            ("pro", "annual"): settings.stripe_price_pro_annual,
            ("business", "monthly"): settings.stripe_price_business_monthly,
            ("business", "annual"): settings.stripe_price_business_annual}


def stripe_data(value):
    """Normalize SDK resources (including nested objects) to plain dictionaries."""
    return value.to_dict() if hasattr(value, "to_dict") else value


def stripe_client():
    import stripe
    live = stripe_live_mode()
    if live and not settings.stripe_live_enabled:
        raise HTTPException(503, "Live payments are not activated")
    if live:
        url = urlsplit(settings.app_base_url)
        if url.scheme != "https" or url.hostname in {None, "localhost", "127.0.0.1", "::1"} or url.path not in {"", "/"} or url.query or url.fragment:
            raise HTTPException(503, "Live payments require a public HTTPS APP_BASE_URL")
    if not settings.stripe_secret_key.startswith("sk_live_" if live else "sk_test_"):
        raise HTTPException(503, "Stripe secret key does not match STRIPE_MODE")
    stripe.api_key = settings.stripe_secret_key
    stripe.api_version = "2025-04-30.basil"
    stripe.max_network_retries = 1
    return stripe


def checked_price(plan, interval):
    stripe = stripe_client()
    price_id = price_ids().get((plan, interval))
    if not price_id:
        raise HTTPException(503, "This subscription price is not configured")
    price = stripe_data(stripe.Price.retrieve(price_id))
    recurring = price.get("recurring") or {}
    if (bool(price.get("livemode")) != stripe_live_mode() or not price.get("active") or price.get("currency") != "usd" or
            price.get("unit_amount") != price_cents(plan, interval) or
            recurring.get("interval") != ("month" if interval == "monthly" else "year") or
            recurring.get("interval_count") != 1 or recurring.get("usage_type") != "licensed"):
        raise HTTPException(503, "Stripe price does not match the approved subscription catalog")
    return price_id


def lock_operation(uid, operation):
    from google.cloud import firestore
    ref = subscription_ref(uid)
    @firestore.transactional
    def claim(tx):
        data = next(tx.get(ref)).to_dict() or {}
        now = utcnow()
        if data.get("operation_until", now) > now:
            raise HTTPException(409, "A billing change is already in progress. Try again shortly.")
        token = str(uuid.uuid4())
        tx.set(ref, {"operation_token": token, "operation_until": now + timedelta(minutes=5), "operation": operation}, merge=True)
        return data, token
    return claim(client().transaction())


def finish_operation(uid, token, fields=None):
    from google.cloud import firestore
    ref = subscription_ref(uid)
    @firestore.transactional
    def finish(tx):
        data = next(tx.get(ref)).to_dict() or {}
        if data.get("operation_token") == token:
            tx.set(ref, {**(fields or {}), "operation_until": utcnow(), "operation_token": None}, merge=True)
    finish(client().transaction())


def customer(uid, data):
    stripe = stripe_client()
    customer_id = data.get("customer_id")
    if not customer_id:
        customer_id = stripe_data(stripe.Customer.create(metadata={"user_id": uid}, idempotency_key=f"highlights-customer:{settings.stripe_mode}:{uid}"))["id"]
        subscription_ref(uid).set({"customer_id": customer_id}, merge=True)
    return customer_id


def checkout(uid, plan, interval):
    if not enabled():
        raise HTTPException(503, "Subscriptions are not available yet")
    price = checked_price(plan, interval)
    stripe = stripe_client()
    data, token = lock_operation(uid, "checkout")
    try:
        cus = customer(uid, data)
        # Stripe is checked as well as the projection, including incomplete sessions.
        subs = stripe_data(stripe.Subscription.list(customer=cus, status="all", limit=100))["data"]
        if any(s["status"] not in TERMINAL for s in subs):
            raise HTTPException(409, "You already have a subscription. Change your plan from account settings.")
        for pending in stripe_data(stripe.checkout.Session.list(customer=cus, limit=100))["data"]:
            if pending.get("mode") == "subscription" and pending.get("status") == "open" and pending.get("client_reference_id") == uid:
                if pending.get("metadata", {}).get("price_id") == price:
                    return {"url": pending["url"]}
                stripe.checkout.Session.expire(pending["id"])
        if data.get("checkout_id"):
            previous = stripe_data(stripe.checkout.Session.retrieve(data["checkout_id"]))
            if previous.get("status") == "open":
                if data.get("checkout_price") == price:
                    return {"url": previous["url"]}
                stripe.checkout.Session.expire(previous["id"])
            elif previous.get("status") == "complete":
                previous_sub = object_id(previous.get("subscription"))
                if not previous_sub or stripe_data(stripe.Subscription.retrieve(previous_sub))["status"] not in TERMINAL:
                    raise HTTPException(409, "Your payment is being confirmed. Refresh account settings shortly.")
        session = stripe_data(stripe.checkout.Session.create(mode="subscription", customer=cus,
            line_items=[{"price": price, "quantity": 1}], client_reference_id=uid,
            metadata={"user_id": uid, "price_id": price}, subscription_data={"metadata": {"user_id": uid}},
            success_url=f"{settings.app_base_url}/account?payment=success",
            cancel_url=f"{settings.app_base_url}/pricing?payment=cancelled",
            idempotency_key=f"highlights-checkout:{uid}:{token}"))
        finish_operation(uid, token, {"checkout_id": session["id"], "checkout_price": price})
        return {"url": session["url"]}
    finally:
        finish_operation(uid, token)


def portal(uid):
    stripe = stripe_client()
    data, token = lock_operation(uid, "portal")
    try:
        if not data.get("customer_id"):
            raise HTTPException(409, "No billing account exists yet")
        config_id = settings.stripe_portal_configuration_id
        if not config_id:
            raise HTTPException(503, "Stripe Customer Portal is not configured")
        config = stripe_data(stripe.billing_portal.Configuration.retrieve(config_id))
        features = config.get("features", {})
        if (not config.get("active") or bool(config.get("livemode")) != stripe_live_mode() or features.get("subscription_update", {}).get("enabled") or
                not features.get("subscription_cancel", {}).get("enabled") or
                features["subscription_cancel"].get("mode") != "at_period_end"):
            raise HTTPException(503, "Portal must disable plan changes and use period-end cancellation")
        # Portal cannot cancel schedule-managed subscriptions. Release the schedule
        # first, preserving the current paid plan, and disclose this in the UI.
        if data.get("schedule_id"):
            stripe.SubscriptionSchedule.release(data["schedule_id"], preserve_cancel_date=True)
            subscription_ref(uid).set({"schedule_id": None, "scheduled_change": None}, merge=True)
        session = stripe_data(stripe.billing_portal.Session.create(customer=data["customer_id"], configuration=config_id,
                                                        return_url=f"{settings.app_base_url}/account"))
        return {"url": session["url"]}
    finally:
        finish_operation(uid, token)


def upgrade_source(uid, data, stripe):
    if not data.get("subscription_id"):
        raise HTTPException(409, "Subscribe before changing plans")
    sub = stripe_data(stripe.Subscription.retrieve(data["subscription_id"], expand=["latest_invoice"]))
    if object_id(sub.get("customer")) != data.get("customer_id") or sub.get("metadata", {}).get("user_id") != uid:
        raise HTTPException(400, "Subscription ownership is invalid")
    if sub["status"] != "active" or sub.get("cancel_at_period_end") or sub.get("pause_collection"):
        raise HTTPException(409, "Resolve payment or cancellation before changing plans")
    if sub.get("pending_update"):
        raise HTTPException(409, "Complete the pending upgrade payment before making another change")
    items = sub["items"]["data"]
    if len(items) != 1 or items[0].get("quantity") != 1:
        raise HTTPException(409, "This subscription cannot be changed automatically")
    return sub, items[0]


def upgrade_preview(uid, plan, interval):
    price = checked_price(plan, interval)
    stripe = stripe_client()
    data, token = lock_operation(uid, "upgrade-preview")
    try:
        sub, item = upgrade_source(uid, data, stripe)
        source = next((pair for pair, pid in price_ids().items() if pid == item["price"]["id"]), None)
        if not source or source[0] != "pro" or plan != "business" or source[1] != interval:
            raise HTTPException(409, "Immediate upgrades use your current billing frequency. Other changes take effect at renewal.")
        balance, projection = load(uid)
        now = utcnow()
        if not paid_access(projection, now):
            raise HTTPException(409, "Wait for your current payment to be confirmed before upgrading")
        proration_date = int(now.timestamp())
        invoice = stripe_data(stripe.Invoice.create_preview(subscription=sub["id"], subscription_details={
            "items": [{"id": item["id"], "price": price, "quantity": 1}],
            "proration_date": proration_date, "proration_behavior": "always_invoice"}))
        start, end = balance["allowance_start"], balance["allowance_end"]
        remaining = max(0, int(end.timestamp()) - proration_date)
        duration = int((end - start).total_seconds())
        extra = (PLANS[plan]["minutes"] - PLANS[source[0]]["minutes"]) * 1000 * min(remaining, duration) // duration
        quote = {"id": str(uuid.uuid4()), "subscription_id": sub["id"], "from_price": item["price"]["id"],
                 "price_id": price, "plan": plan, "interval": interval, "proration_date": proration_date,
                 "period_start": item.get("current_period_start") or sub["current_period_start"],
                 "period_end": item.get("current_period_end") or sub["current_period_end"],
                 "allowance_key": balance["allowance_key"], "allowance_end": end,
                 "extra_milli": extra, "amount_due": invoice["amount_due"], "currency": invoice["currency"],
                 "expires_at": now + timedelta(minutes=5)}
        finish_operation(uid, token, {"upgrade_quote": quote})
        return {"quote_id": quote["id"], "plan": plan, "interval": interval, "amount_due": quote["amount_due"],
                "currency": quote["currency"], "extra_minutes": extra / 1000, "allowance_end": iso(end),
                "expires_at": iso(quote["expires_at"])}
    finally:
        finish_operation(uid, token)


def upgrade_now(uid, plan, interval, quote_id):
    price = checked_price(plan, interval)
    stripe = stripe_client()
    data, token = lock_operation(uid, "upgrade")
    try:
        quote = data.get("upgrade_quote") or {}
        if (quote.get("id") != quote_id or quote.get("price_id") != price or
                quote.get("subscription_id") != data.get("subscription_id")):
            raise HTTPException(409, "Review a fresh upgrade quote before confirming")
        sub = stripe_data(stripe.Subscription.retrieve(data["subscription_id"], expand=["latest_invoice"]))
        if object_id(sub.get("customer")) != data.get("customer_id") or sub.get("metadata", {}).get("user_id") != uid:
            raise HTTPException(400, "Subscription ownership is invalid")
        item = sub["items"]["data"][0]
        # A lost response must not create another invoice. Reconcile the same quote.
        already_requested = (data.get("upgrade_request") or {}).get("id") == quote_id
        already_applied = item["price"]["id"] == price
        if already_requested and not already_applied and not sub.get("pending_update") and (quote["expires_at"] <= utcnow() or quote["period_end"] <= int(utcnow().timestamp())):
            raise HTTPException(409, "The pending upgrade expired. Review a fresh quote before trying again.")
        if not already_requested and not already_applied:
            sub, item = upgrade_source(uid, data, stripe)
            if (quote["expires_at"] <= utcnow() or quote["allowance_end"] <= utcnow() or quote["period_end"] <= int(utcnow().timestamp()) or
                    item["price"]["id"] != quote["from_price"] or
                    (item.get("current_period_start") or sub.get("current_period_start")) != quote["period_start"] or
                    (item.get("current_period_end") or sub.get("current_period_end")) != quote["period_end"]):
                raise HTTPException(409, "The upgrade quote expired or your subscription changed. Review a fresh quote.")
            invoice = stripe_data(stripe.Invoice.create_preview(subscription=sub["id"], subscription_details={
                "items": [{"id": item["id"], "price": price, "quantity": 1}],
                "proration_date": quote["proration_date"], "proration_behavior": "always_invoice"}))
            if invoice["amount_due"] != quote["amount_due"] or invoice["currency"] != quote["currency"]:
                raise HTTPException(409, "The payment amount changed. Review a fresh upgrade quote.")
            # Persist proof before Stripe can emit its first payment webhook.
            subscription_ref(uid).set({"upgrade_request": quote}, merge=True)
            already_requested = True
        if not already_applied and not sub.get("pending_update"):
            if sub.get("schedule"):
                stripe.SubscriptionSchedule.release(object_id(sub["schedule"]), preserve_cancel_date=True)
                subscription_ref(uid).set({"schedule_id": None, "scheduled_change": None}, merge=True)
            sub = stripe_data(stripe.Subscription.modify(sub["id"],
                items=[{"id": item["id"], "price": price, "quantity": 1}],
                payment_behavior="pending_if_incomplete", proration_behavior="always_invoice",
                proration_date=quote["proration_date"], expand=["latest_invoice"],
                idempotency_key=f"highlights-upgrade:{uid}:{quote_id}"))
        invoice = sub.get("latest_invoice") or {}
        if isinstance(invoice, str):
            invoice = stripe_data(stripe.Invoice.retrieve(invoice))
        pending = bool(sub.get("pending_update"))
        sync_subscription(uid, sub["id"], {"id": f"upgrade-response:{quote_id}:{invoice.get('id')}:{pending}", "type": "upgrade.response"})
        return {"scheduled_change": None, "status": "payment_required" if pending else "upgraded" if account(uid)["plan"] == "business" else "confirming",
                "payment_url": invoice.get("hosted_invoice_url") if pending else None}
    finally:
        finish_operation(uid, token)


def schedule_change(uid, plan, interval, quote_id=None):
    if quote_id:
        return upgrade_now(uid, plan, interval, quote_id)
    price = checked_price(plan, interval)
    stripe = stripe_client()
    data, token = lock_operation(uid, "change")
    try:
        if not data.get("subscription_id"):
            raise HTTPException(409, "Subscribe before changing plans")
        sub = stripe_data(stripe.Subscription.retrieve(data["subscription_id"]))
        if sub["status"] != "active" or sub.get("cancel_at_period_end") or sub.get("pause_collection"):
            raise HTTPException(409, "Resolve payment or cancellation before changing plans")
        if sub.get("pending_update"):
            raise HTTPException(409, "Complete the pending upgrade payment before making another change")
        item = sub["items"]["data"][0]
        source = next((pair for pair, pid in price_ids().items() if pid == item["price"]["id"]), None)
        if source == ("pro", interval) and plan == "business":
            raise HTTPException(409, "Review and confirm the prorated upgrade before upgrading now")
        end = item.get("current_period_end") or sub.get("current_period_end")
        start = item.get("current_period_start") or sub.get("current_period_start")
        current_price = item["price"]["id"]
        if price == current_price:
            if sub.get("schedule"):
                stripe.SubscriptionSchedule.release(object_id(sub["schedule"]), preserve_cancel_date=True)
            finish_operation(uid, token, {"schedule_id": None, "scheduled_change": None})
            return {"scheduled_change": None}
        schedule_id = object_id(sub.get("schedule"))
        if not schedule_id:
            schedule_id = stripe_data(stripe.SubscriptionSchedule.create(from_subscription=sub["id"],
                idempotency_key=f"highlights-schedule:{sub['id']}:{token}"))["id"]
        stripe.SubscriptionSchedule.modify(schedule_id, end_behavior="release", proration_behavior="none", phases=[
            {"start_date": start, "end_date": end, "items": [{"price": current_price, "quantity": 1}], "proration_behavior": "none"},
            {"start_date": end, "iterations": 1, "items": [{"price": price, "quantity": 1}], "proration_behavior": "none"},
        ], idempotency_key=f"highlights-change:{uid}:{token}")
        change = {"plan": plan, "interval": interval, "effective_at": iso(timestamp(end))}
        finish_operation(uid, token, {"schedule_id": schedule_id, "scheduled_change": change})
        return {"scheduled_change": change}
    finally:
        finish_operation(uid, token)


def object_id(value):
    return value.get("id") if isinstance(value, dict) else value


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc) if value else None


def invoice_subscription(invoice):
    return object_id(invoice.get("subscription") or (invoice.get("parent") or {}).get("subscription_details", {}).get("subscription"))


def sync_subscription(uid, sub_id, event):
    """Read canonical Stripe state inside the transaction so a retried write refetches.

    Grants follow the current paid invoice, never the incoming event's stale object.
    """
    from google.cloud import firestore
    stripe = stripe_client()
    ref, accountref = subscription_ref(uid), account_ref(uid)
    eventref = client().collection("stripe_events").document(event["id"])
    @firestore.transactional
    def sync(tx):
        old = next(tx.get(ref)).to_dict() or {}
        balance = next(tx.get(accountref)).to_dict() or {}
        receipt = next(tx.get(eventref))
        if receipt.exists:
            return
        sub = stripe_data(stripe.Subscription.retrieve(sub_id, expand=["latest_invoice"]))
        if object_id(sub.get("customer")) != old.get("customer_id") or sub.get("metadata", {}).get("user_id") != uid:
            raise HTTPException(400, "Subscription ownership is invalid")
        if old.get("subscription_id") not in (None, sub_id):
            if sub["status"] in TERMINAL:
                tx.set(eventref, {"user_id": uid, "type": event["type"], "processed_at": utcnow(), "obsolete": True})
                return
            if old.get("status") not in TERMINAL:
                raise HTTPException(409, "Another subscription is already associated with this account")
        items = sub["items"]["data"]
        if len(items) != 1 or items[0].get("quantity") != 1:
            raise HTTPException(400, "Unexpected subscription items")
        item = items[0]
        pair = next((pair for pair, pid in price_ids().items() if pid == item["price"]["id"]), None)
        if not pair:
            raise HTTPException(400, "Unknown subscription price")
        plan, interval = pair
        invoice = sub.get("latest_invoice") or {}
        if isinstance(invoice, str):
            invoice = stripe_data(stripe.Invoice.retrieve(invoice))
        start = timestamp(item.get("current_period_start") or sub.get("current_period_start"))
        end = timestamp(item.get("current_period_end") or sub.get("current_period_end"))
        if not start or not end:
            raise HTTPException(400, "Subscription period is missing")
        fields = {"subscription_id": sub_id, "customer_id": object_id(sub["customer"]), "price_id": item["price"]["id"],
                  "plan": plan, "interval": interval, "status": sub["status"], "current_period_end": end,
                  "cancel_at_period_end": bool(sub.get("cancel_at_period_end")), "pause_collection": bool(sub.get("pause_collection")),
                  "updated_at": utcnow()}
        invoice_paid_for_period = False
        lines = invoice.get("lines") or {}
        if invoice.get("status") == "paid" and invoice.get("billing_reason") != "subscription_update":
            lines = invoice.get("lines") or stripe_data(stripe.Invoice.list_lines(invoice["id"], limit=100))
            invoice_paid_for_period = any(
                object_id(line.get("price") or (line.get("pricing") or {}).get("price_details", {}).get("price")) == item["price"]["id"] and
                (line.get("period") or {}).get("start") == int(start.timestamp()) and
                (line.get("period") or {}).get("end") == int(end.timestamp())
                for line in lines.get("data", []))
        request = old.get("upgrade_request") or {}
        upgrade_paid = (invoice.get("status") == "paid" and invoice.get("billing_reason") == "subscription_update" and
                        sub["status"] == "active" and not sub.get("pending_update") and
                        request.get("price_id") == item["price"]["id"] and old.get("paid_plan", old.get("plan")) == "pro" and
                        plan == "business" and request.get("interval") == interval and
                        any(object_id(line.get("price") or (line.get("pricing") or {}).get("price_details", {}).get("price")) == request["price_id"] and
                            (line.get("period") or {}).get("start") == request["proration_date"] and
                            (line.get("period") or {}).get("end") == request["period_end"] and line.get("amount", 0) >= 0
                            for line in lines.get("data", [])))
        upgrade_receipt = ledger_ref(uid, f"upgrade:{invoice.get('id')}")
        upgrade_seen = next(tx.get(upgrade_receipt)).exists if upgrade_paid else True
        if upgrade_paid:
            fields.update(paid_plan=plan, paid_invoice_id=invoice["id"], upgrade_request=None, grace_until=None)
        fields["pending_upgrade"] = ({"plan": request.get("plan", "business"), "payment_url": invoice.get("hosted_invoice_url"),
                                      "expires_at": iso(timestamp(sub["pending_update"]["expires_at"]))}
                                     if sub.get("pending_update") and request else None)
        if invoice_paid_for_period and sub["status"] == "active":
            fields.update(paid_until=end, allowance_anchor=start, paid_plan=plan, grace_until=None, paid_invoice_id=invoice["id"])
        elif sub["status"] == "past_due":
            failed_at = timestamp(invoice.get("created")) or end
            fields["grace_until"] = failed_at + timedelta(days=3)
        elif sub["status"] in TERMINAL | {"unpaid", "paused", "incomplete"}:
            fields.update(grace_until=None, paid_until=None)
        schedule_id = object_id(sub.get("schedule"))
        fields.update(schedule_id=schedule_id, scheduled_change=None)
        if schedule_id:
            schedule = stripe_data(stripe.SubscriptionSchedule.retrieve(schedule_id))
            for phase in schedule.get("phases", []):
                if phase["start_date"] >= int(end.timestamp()):
                    target = object_id(phase["items"][0]["price"])
                    target_pair = next((p for p, pid in price_ids().items() if pid == target), None)
                    if target_pair:
                        fields["scheduled_change"] = {"plan": target_pair[0], "interval": target_pair[1],
                                                      "effective_at": iso(timestamp(phase["start_date"]))}
                    break
        updated = refreshed(balance, {**old, **fields}, utcnow())
        if upgrade_paid and not upgrade_seen:
            extra = int(request["extra_milli"]) if updated.get("allowance_key") == request["allowance_key"] and utcnow() < request["allowance_end"] else 0
            updated["subscription_available_milli"] = int(updated.get("subscription_available_milli", 0)) + extra
            updated["allowance_grant_milli"] = int(updated.get("allowance_grant_milli", 0)) + extra
            tx.set(upgrade_receipt, {"kind": "subscription_upgrade", "invoice_id": invoice["id"], "quote_id": request["id"],
                                     "available_delta_milli": extra, "reserved_delta_milli": 0, "created_at": utcnow()})
        record_allowance(tx, uid, balance, updated)
        tx.set(ref, fields, merge=True)
        tx.set(accountref, updated, merge=True)
        tx.set(eventref, {"user_id": uid, "type": event["type"], "processed_at": utcnow()})
    sync(client().transaction())


def webhook(event):
    event = stripe_data(event)
    stripe = stripe_client()
    require_event_mode(event)
    require_live_transition()
    obj, kind = event["data"]["object"], event["type"]
    if kind in {"checkout.session.completed", "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed", "checkout.session.expired"}:
        session = stripe_data(stripe.checkout.Session.retrieve(obj["id"]))
        uid = session.get("metadata", {}).get("user_id")
        if not uid or session.get("client_reference_id") != uid:
            raise HTTPException(400, "Checkout ownership is invalid")
        if session.get("mode") == "subscription":
            if session.get("subscription"):
                sync_subscription(uid, object_id(session["subscription"]), event)
            return
        if session.get("payment_status") != "paid":
            return
        grant_pack(uid, session, event)
        return
    sub_id = None
    if kind.startswith("customer.subscription."):
        sub_id = obj["id"]
    elif kind in {"invoice.paid", "invoice.payment_failed", "invoice.payment_action_required"}:
        sub_id = invoice_subscription(obj)
    elif kind.startswith("subscription_schedule."):
        sub_id = object_id(obj.get("subscription") or obj.get("released_subscription"))
    if not sub_id:
        return
    sub = stripe_data(stripe.Subscription.retrieve(sub_id))
    uid = sub.get("metadata", {}).get("user_id")
    if not uid:
        return  # unrelated Stripe products
    sync_subscription(uid, sub_id, event)


def grant_pack(uid, session, event):
    from google.cloud import firestore
    stripe = stripe_client()
    # Snapshot minutes into Checkout metadata at creation; never use mutable pack
    # config at webhook time. Verify actual paid line item and USD amount as well.
    if session.get("metadata", {}).get("kind") != "topup":
        raise HTTPException(400, "Unknown prepaid checkout")
    lines = stripe_data(stripe.checkout.Session.list_line_items(session["id"], limit=2))["data"]
    if (len(lines) != 1 or lines[0]["price"]["id"] != settings.stripe_price_topup or
            session.get("currency") != "usd" or session.get("amount_total") != TOPUP["amount_cents"] or
            session.get("metadata", {}).get("minutes") != str(TOPUP["minutes"])):
        raise HTTPException(400, "Top-up does not match the approved catalog")
    ref, receipt = account_ref(uid), ledger_ref(uid, f"stripe:{session['id']}")
    eventref = client().collection("stripe_events").document(event["id"])
    @firestore.transactional
    def grant(tx):
        balance = next(tx.get(ref)).to_dict() or {}
        require_balance_mode(balance)
        seen = next(tx.get(receipt)).exists
        if not seen:
            tx.set(ref, {"available_milli": int(balance.get("available_milli", 0)) + TOPUP["minutes"] * 1000}, merge=True)
            tx.set(receipt, {"kind": "purchase", "amount_milli": TOPUP["minutes"] * 1000, "created_at": utcnow(), "stripe_session_id": session["id"]})
        tx.set(eventref, {"user_id": uid, "type": event["type"], "processed_at": utcnow()})
    grant(client().transaction())


def topup(uid):
    if not account(uid)["can_top_up"]:
        raise HTTPException(402, "Video-minute top-ups are available to subscribers")
    stripe = stripe_client()
    price = stripe_data(stripe.Price.retrieve(settings.stripe_price_topup))
    if (bool(price.get("livemode")) != stripe_live_mode() or not price.get("active") or price.get("currency") != "usd" or
            price.get("unit_amount") != TOPUP["amount_cents"] or price.get("recurring")):
        raise HTTPException(503, "Top-up price must be a $9.99 USD one-time price")
    data, token = lock_operation(uid, "topup")
    try:
        cus = customer(uid, data)
        for pending in stripe_data(stripe.checkout.Session.list(customer=cus, limit=100))["data"]:
            if pending.get("mode") == "payment" and pending.get("status") == "open" and pending.get("metadata", {}).get("kind") == "topup":
                return {"url": pending["url"]}
        if data.get("topup_checkout_id"):
            previous = stripe_data(stripe.checkout.Session.retrieve(data["topup_checkout_id"]))
            if previous.get("status") == "open":
                return {"url": previous["url"]}
        session = stripe_data(stripe.checkout.Session.create(mode="payment", customer=cus,
            line_items=[{"price": settings.stripe_price_topup, "quantity": 1}], client_reference_id=uid,
            metadata={"user_id": uid, "pack_key": TOPUP["id"], "kind": "topup", "minutes": str(TOPUP["minutes"])},
            success_url=f"{settings.app_base_url}/account?payment=success",
            cancel_url=f"{settings.app_base_url}/account?payment=cancelled",
            idempotency_key=f"highlights-topup:{uid}:{token}"))
        finish_operation(uid, token, {"topup_checkout_id": session["id"]})
        return {"url": session["url"]}
    finally:
        finish_operation(uid, token)


def refresh_all():
    if not enabled():
        return 0
    count = 0
    for item in client().collection_group("account").stream():
        if item.id == ("subscription-live" if stripe_live_mode() else "subscription") and item.to_dict().get("subscription_id"):
            load(item.reference.path.split("/")[-3])
            count += 1
    return count


def transition_live(*, verify=False, dry_run=False):
    """Archive sandbox grants; preserve files, legacy credit and trial history.

    No Stripe calls or subscription cancellations are made by this migration.
    Execute only under billing maintenance before live payments are enabled.
    """
    from google.cloud import firestore
    if not stripe_live_mode() or settings.stripe_live_enabled or not settings.billing_migration_mode:
        raise HTTPException(409, "Use live mode with billing maintenance enabled and live payments disabled for transition")
    db = client()
    for item in db.collection_group("runs").stream():
        row = item.to_dict() or {}
        if row.get("status") in {"queued", "running", "cancel_requested"} or (row.get("reel_job") or {}).get("status") in {"queued", "rendering"}:
            raise HTTPException(409, "Wait for active analyses and reels to finish before transitioning billing")
    accounts = [item for item in db.collection_group("account").stream() if item.id == "credits"]
    if any(int((item.to_dict() or {}).get("reserved_milli", 0)) for item in accounts):
        raise HTTPException(409, "Resolve reserved credits before transitioning billing")
    totals = {"sandbox_subscription_milli": 0, "sandbox_prepaid_milli": 0, "preserved_legacy_milli": 0}
    changed = missing = 0
    marker = db.collection("maintenance").document("stripe-live-v1")
    if not dry_run and not verify:
        marker.set({"verified": False}, merge=True)
    for item in accounts:
        row = item.to_dict() or {}
        totals["preserved_legacy_milli"] += int(row.get("legacy_available_milli", 0))
        if row.get("stripe_mode") == "live":
            continue
        totals["sandbox_subscription_milli"] += int(row.get("subscription_available_milli", 0))
        totals["sandbox_prepaid_milli"] += int(row.get("available_milli", 0))
        if dry_run or verify:
            missing += 1
            continue
        archive = item.reference.collection("environment_archive").document("stripe-test-v1")
        @firestore.transactional
        def apply(tx):
            current = next(tx.get(item.reference)).to_dict() or {}
            saved = next(tx.get(archive))
            if current.get("stripe_mode") == "live":
                return 0
            if int(current.get("reserved_milli", 0)):
                raise HTTPException(409, "Credit reservation changed during transition; retry after draining")
            if not saved.exists:
                tx.set(archive, {"credits": current, "archived_at": utcnow(), "from_mode": "test"})
            tx.set(item.reference, {"stripe_mode": "live", "subscription_available_milli": 0, "available_milli": 0,
                                   "expired_subscription_milli": 0, "expired_allowance_key": None,
                                   "allowance_key": None, "allowance_start": None, "allowance_end": None,
                                   "allowance_grant_milli": 0}, merge=True)
            return 1
        changed += apply(db.transaction())
    if verify and not dry_run and missing == 0:
        marker.set({"verified": True, "verified_at": utcnow()}, merge=True)
    return {"dry_run": dry_run, "changed": changed, "missing": missing,
            "verified": bool(verify and not dry_run and missing == 0), **totals}


def migrate(verify=False):
    """Run while subscription flag is off; repeat safely after any failed pass."""
    from google.cloud import firestore
    if settings.subscriptions_enabled or not settings.billing_migration_mode:
        raise HTTPException(409, "Disable subscriptions and enable billing maintenance during migration")
    db = client()
    marker = db.collection("maintenance").document("subscriptions-v1")
    marker.set({"verified": False}, merge=True)
    for item in db.collection_group("runs").stream():
        row = item.to_dict() or {}
        if row.get("status") in {"queued", "running", "cancel_requested"} or (row.get("reel_job") or {}).get("status") in {"queued", "rendering"}:
            raise HTTPException(409, "Wait for active analyses and reels to finish before migrating")
    changed = missing = 0
    for item in db.collection_group("account").stream():
        if item.id != "credits":
            continue
        @firestore.transactional
        def migrate_account(tx):
            row = next(tx.get(item.reference)).to_dict() or {}
            if row.get("billing_migrated"):
                return 0
            if verify:
                return 1
            tx.set(item.reference, {"legacy_available_milli": int(row.get("available_milli", 0)),
                                   "available_milli": 0, "billing_migrated": True}, merge=True)
            return 1
        value = migrate_account(db.transaction())
        missing += value if verify else 0
        changed += value if not verify else 0
    for item in db.collection_group("runs").stream():
        @firestore.transactional
        def migrate_run(tx):
            row = next(tx.get(item.reference)).to_dict() or {}
            if row.get("billing_migrated"):
                return 0
            if verify:
                return 1
            tx.update(item.reference, {"legacy_access": True, "billing_migrated": True,
                                      "lineage_id": row.get("lineage_id") or item.id})
            return 1
        value = migrate_run(db.transaction())
        missing += value if verify else 0
        changed += value if not verify else 0
    if verify and missing == 0:
        marker.set({"verified": True, "verified_at": utcnow()}, merge=True)
    return {"changed": changed, "missing": missing, "verified": verify and missing == 0}


def reel_fingerprint(windows, format, sponsors, combine_overlaps=True, exclude_free_throws=False):
    # Preserve existing combined-export cache keys, but distinguish user choice.
    payload = [windows, format, sponsors]
    if not combine_overlaps:
        payload.append({"combine_overlaps": False})
    if exclude_free_throws:
        payload.append({"exclude_free_throws": True})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def record_allowance(tx, uid, previous, updated):
    if updated.get("free_granted") and not previous.get("free_granted"):
        tx.set(ledger_ref(uid, "free-trial"), {"kind": "free_trial", "amount_milli": 15000, "created_at": utcnow()})
    key = updated.get("allowance_key")
    if key and key != previous.get("allowance_key"):
        receipt = ledger_ref(uid, "allowance:" + hashlib.sha256(key.encode()).hexdigest())
        tx.set(receipt, {"kind": "subscription_allowance", "allowance_key": key,
                         "amount_milli": updated["allowance_grant_milli"],
                         "expires_at": updated["allowance_end"], "created_at": utcnow()})
