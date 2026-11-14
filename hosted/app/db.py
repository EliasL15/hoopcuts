from __future__ import annotations

from datetime import datetime, timezone

from .firebase import client


def server_timestamp():
    from google.cloud.firestore import SERVER_TIMESTAMP

    return SERVER_TIMESTAMP


def user_ref(user_id: str):
    return client().collection("users").document(user_id)


def account_ref(user_id: str):
    return user_ref(user_id).collection("account").document("credits")


def runs_ref(user_id: str):
    return user_ref(user_id).collection("runs")


def run_ref(user_id: str, run_id: str):
    return runs_ref(user_id).document(run_id)


def events_ref(user_id: str, run_id: str):
    return run_ref(user_id, run_id).collection("events")


def event_ref(user_id: str, run_id: str, event_id: str):
    return events_ref(user_id, run_id).document(event_id)


def ledger_ref(user_id: str, event_key: str):
    return user_ref(user_id).collection("credit_ledger").document(event_key)


def outbox_ref(run_id: str):
    return client().collection("dispatch_outbox").document(run_id)


def isoformat(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def utcnow():
    return datetime.now(timezone.utc)


def read_run(user_id: str, run_id: str) -> dict | None:
    snapshot = run_ref(user_id, run_id).get()
    if not snapshot.exists:
        return None
    return {"id": snapshot.id, **(snapshot.to_dict() or {})}


def read_events(user_id: str, run_id: str) -> list[dict]:
    rows = [{"id": item.id, **(item.to_dict() or {})} for item in events_ref(user_id, run_id).stream()]
    rows.sort(key=lambda row: (float(row.get("timestamp_seconds", 0)), row["id"]))
    return rows
