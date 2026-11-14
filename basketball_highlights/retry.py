"""Shared request cooldown and typed, safe failure reporting."""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)
BILLING_ERRORS = {
    "credit_balance_exhausted": "OpenAI API credits are exhausted. Add API credits before retrying.",
    "organization_spend_limit_exceeded": "The OpenAI organization spend limit was reached. Update that limit before retrying.",
    "project_spend_limit_exceeded": "The OpenAI project spend limit was reached. Update that limit before retrying.",
    "organization_usage_limit_exceeded": "The OpenAI organization usage limit was reached. Request a higher limit before retrying.",
}


class RequestFailed(RuntimeError):
    def __init__(self, category: str, retryable: bool, status_code=None, retry_after=None, error_code=None):
        self.category, self.retryable = category, retryable
        self.status_code, self.retry_after = status_code, retry_after
        self.error_code = error_code
        super().__init__(BILLING_ERRORS.get(error_code, f"Decisions request failed: {category}"))


def failure(exc: Exception) -> RequestFailed:
    if isinstance(exc, RequestFailed):
        return exc
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None) or {}
    error = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    code = getattr(exc, "code", None) or error.get("code")
    error_type = getattr(exc, "type", None) or error.get("type")
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    delay = None
    value = headers.get("retry-after")
    try:
        delay = max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            delay = max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    if code in BILLING_ERRORS or code in {"insufficient_quota", "billing_hard_limit_reached", "quota_exceeded"} or error_type == "insufficient_quota":
        return RequestFailed("quota_exhausted", False, status, delay,
                             error_code=code if code in BILLING_ERRORS else None)
    if status == 429:
        return RequestFailed("rate_limit", True, status, delay)
    if status in {401, 403}:
        return RequestFailed("authentication" if status == 401 else "permission", False, status)
    if status in {408, 409, 500, 502, 503, 504} or status is None and not isinstance(exc, (ValueError, TypeError)):
        return RequestFailed("temporary_api_error", True, status, delay)
    return RequestFailed("invalid_request", False, status)


class RequestGate:
    def __init__(self, maximum: int, *, clock=time.monotonic, sleep=time.sleep, on_wait=None):
        self.maximum = self.concurrency = maximum
        self.clock, self.sleep = clock, sleep
        self.on_wait = on_wait
        self.last_notification = float("-inf")
        self.lock = threading.Lock()
        self.until = 0.0
        self.active = self.successes = 0
        self.terminal: RequestFailed | None = None
        self.interval = self.next_start = 0.0
        self.performance = None

    def observe_headers(self, headers):
        """Use reported request capacity with headroom; honor exhausted resets."""
        def number(name):
            try:
                return float(headers.get(name))
            except (TypeError, ValueError):
                return None
        limit = number("x-ratelimit-limit-requests")
        with self.lock:
            if limit and limit > 0:
                self.interval = 60 / (limit * .8)
            for kind in ("requests", "tokens", "project-tokens"):
                remaining = number(f"x-ratelimit-remaining-{kind}")
                reset = headers.get(f"x-ratelimit-reset-{kind}", "")
                if remaining is not None and remaining <= 0:
                    # Duration headers are expressed in ms/s/m/h, sometimes combined.
                    import re
                    parts = re.findall(r"([\d.]+)(ms|s|m|h)", str(reset))
                    seconds = sum(float(value) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[unit]
                                  for value, unit in parts)
                    self.until = max(self.until, self.clock() + (seconds or 1))

    def acquire(self):
        while True:
            with self.lock:
                if self.terminal:
                    raise self.terminal
                delay = max(self.until, self.next_start) - self.clock()
                if delay <= 0 and self.active < self.concurrency:
                    self.active += 1
                    self.next_start = self.clock() + self.interval
                    return
            notify = False
            with self.lock:
                if self.on_wait and self.clock() - self.last_notification >= 15:
                    self.last_notification = self.clock()
                    notify = True
            if notify:
                try:
                    self.on_wait()
                except Exception as exc:
                    self.abort(exc)
                    raise
            pause = min(1.0, max(0.01, delay)) if delay > 0 else 0.01
            self.sleep(pause)
            if delay > 0 and self.performance:
                self.performance.add("cooldown", pause)

    def abort(self, error):
        with self.lock:
            self.terminal = error

    def release(self):
        with self.lock:
            self.active -= 1

    def succeeded(self):
        with self.lock:
            self.successes += 1
            if self.successes >= 32:
                self.concurrency = min(self.maximum, self.concurrency + 1)
                self.successes = 0

    def failed(self, error: RequestFailed, attempt: int):
        with self.lock:
            self.successes = 0
            if not error.retryable:
                if self.terminal is None:
                    self.terminal = error
                return
            if error.category == "rate_limit":
                self.concurrency = 1
            delay = max(error.retry_after or 0, min(5 * 2 ** attempt, 60))
            delay += random.uniform(0, min(1, delay * .1))
            self.until = max(self.until, self.clock() + delay)
            log.warning("Model cooldown: category=%s seconds=%.1f concurrency=%s", error.category, delay, self.concurrency)
