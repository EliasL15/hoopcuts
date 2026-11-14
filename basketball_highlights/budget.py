"""Persistent, conservative Decisions API budget ledger."""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    amount_usd: float


class Budget:
    # Pricing docs: $0.10 / 1M input tokens; Decisions has no output token charge.
    INPUT_USD_PER_MILLION = 0.10
    # Empirical safety estimate from a 640x360 game run: roughly 1,116 total input
    # tokens for three frames plus prompt. Keep margin, then settle from API usage.
    IMAGE_TOKEN_RESERVE = 400
    TEXT_TOKEN_RESERVE = 300

    def __init__(self, path: Path, limit: float, journal=None):
        self._lock = threading.RLock()
        self.path, self.limit = path, limit
        self.journal = journal
        saved = journal.budget() if journal else None
        if saved is not None or path.exists():
            self.data = saved if saved is not None else json.loads(path.read_text())
            if float(self.data["limit_usd"]) != float(limit):
                raise ValueError("Resume budget differs from saved budget; use the original --budget-usd value")
            self._reconcile_saved_usage()
        else:
            self.data = {"limit_usd": limit, "request_count": 0, "input_tokens": 0,
                         "output_tokens": 0, "image_count": 0, "estimated_spend_usd": 0.0,
                         "actual_spend_usd": 0.0, "requests": [], "pending": []}
            self.save()

    @property
    def spend(self) -> float:
        with self._lock:
            pending = sum(float(item["reserved_usd"]) for item in self.data.get("pending", []))
            return max(float(self.data["estimated_spend_usd"]) + pending, float(self.data["actual_spend_usd"]))

    @property
    def incurred_spend(self) -> float:
        """Known or conservatively estimated usage, excluding unsent reservations."""
        with self._lock:
            return max(float(self.data["estimated_spend_usd"]), float(self.data["actual_spend_usd"]))

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.spend)

    def reserve(self, image_count: int, *, refinement: bool = False, attempts: int = 1,
                ceiling_fraction: float | None = None, text_tokens: int | None = None) -> BudgetReservation:
        with self._lock:
            amount = ((self.TEXT_TOKEN_RESERVE if text_tokens is None else text_tokens) + image_count * self.IMAGE_TOKEN_RESERVE) * attempts * self.INPUT_USD_PER_MILLION / 1_000_000
            fraction = ceiling_fraction if ceiling_fraction is not None else (1.0 if refinement else 0.8)
            if not 0 < fraction <= 1:
                raise ValueError("Budget ceiling fraction must be between zero and one")
            ceiling = self.limit * fraction
            if self.spend + amount > ceiling + 1e-12:
                raise BudgetExceeded(f"Budget reserve ${amount:.4f} exceeds remaining ${self.remaining:.4f}")
            reservation = BudgetReservation(str(uuid.uuid4()), amount)
            self.data.setdefault("pending", []).append({
                "reservation_id": reservation.reservation_id, "reserved_usd": amount,
                "images": image_count, "attempts": attempts,
            })
            self.save()
            return reservation

    def release(self, reservation: BudgetReservation) -> None:
        """Release a reservation when no model request was sent."""
        with self._lock:
            self.data["pending"] = [item for item in self.data.get("pending", [])
                                    if item.get("reservation_id") != reservation.reservation_id]
            self.save()

    def settle(self, reserve: BudgetReservation | float, *, input_tokens: int | None, output_tokens: int | None,
               images: int, request_id: str | None, error: str | None = None,
               attempts: int = 1) -> None:
        with self._lock:
            amount = reserve.amount_usd if isinstance(reserve, BudgetReservation) else float(reserve)
            reservation_id = reserve.reservation_id if isinstance(reserve, BudgetReservation) else None
            pending = self.data.setdefault("pending", [])
            planned_attempts = max(1, attempts)
            for index, item in enumerate(pending):
                if ((reservation_id and item.get("reservation_id") == reservation_id) or
                    (not reservation_id and abs(float(item["reserved_usd"]) - amount) < 1e-12)):
                    planned_attempts = max(1, int(item.get("attempts", attempts)))
                    pending.pop(index)
                    break
            actual = (input_tokens * self.INPUT_USD_PER_MILLION / 1_000_000) if input_tokens is not None else None
            per_attempt_reserve = amount / planned_attempts
            # A successful response reports usage for that call. Failed retry attempts
            # have no usage record, so retain their preflight allowance as estimated cost.
            estimate = (actual if actual is not None else per_attempt_reserve) + per_attempt_reserve * max(0, attempts - 1)
            self.data["estimated_spend_usd"] += estimate
            if actual is not None:
                self.data["actual_spend_usd"] += actual
            self.data["input_tokens"] += input_tokens or 0
            self.data["output_tokens"] += output_tokens or 0
            self.data["image_count"] += images * attempts
            self.data["request_count"] += attempts
            self.data["requests"].append({"request_id": request_id, "reservation_id": reservation_id,
                "reserved_usd": amount, "estimated_usd": estimate, "actual_usd": actual,
                "input_tokens": input_tokens, "output_tokens": output_tokens, "images": images * attempts,
                "attempts": attempts, "error": error})
            self.save()

    def _reconcile_saved_usage(self) -> None:
        """Replace old worst-case reservations with observed API usage when available."""
        # Reservations still pending at process start belonged to requests interrupted
        # by a crash. Account for their full reserve conservatively before resuming.
        pending = self.data.pop("pending", [])
        self.data["pending"] = []
        for item in pending:
            amount = float(item.get("reserved_usd", 0.0))
            attempts = max(1, int(item.get("attempts", 1)))
            self.data.setdefault("requests", []).append({
                "request_id": None, "reservation_id": item.get("reservation_id"),
                "reserved_usd": amount, "estimated_usd": amount, "actual_usd": None,
                "input_tokens": None, "output_tokens": None,
                "images": int(item.get("images", 0)) * attempts, "attempts": attempts,
                "error": "interrupted request; charged full reserve",
            })
            self.data["request_count"] = int(self.data.get("request_count", 0)) + attempts
            self.data["image_count"] = int(self.data.get("image_count", 0)) + int(item.get("images", 0)) * attempts
        requests = self.data.get("requests", [])
        estimated = 0.0
        actual = 0.0
        for item in requests:
            actual_usd = item.get("actual_usd")
            if item.get("estimated_usd") is not None:
                estimated += float(item["estimated_usd"])
            elif actual_usd is not None:
                estimated += float(actual_usd)
            else:
                estimated += float(item.get("reserved_usd", 0.0))
            if actual_usd is not None:
                actual += float(actual_usd)
        self.data["actual_spend_usd"] = actual
        self.data["estimated_spend_usd"] = estimated
        self.save()

    def save(self) -> None:
        with self._lock:
            if self.journal is not None:
                self.journal.save_budget(self.data)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.data, indent=2))
            temp.replace(self.path)
