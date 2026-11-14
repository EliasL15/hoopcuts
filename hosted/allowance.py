"""Shared request/token capacity, leased in small blocks across Cloud Run jobs."""
from __future__ import annotations

import threading
import time

from .app.db import client


class HostedAllowance:
    def __init__(self, rpm, tpm, check_owner, *, clock=time.time, sleep=time.sleep):
        if rpm <= 0 or tpm <= 0:
            raise ValueError("Both shared RPM and TPM limits must be positive")
        self.rpm, self.tpm = rpm * .8, tpm * .8
        self.check_owner, self.clock, self.sleep = check_owner, clock, sleep
        self.lock = threading.Lock()
        self.requests = self.tokens = 0
        self.expires = 0
        self.ref = client().collection("maintenance").document("decisions-throughput")

    def __call__(self, kwargs):
        content = kwargs["input"][0]["content"]
        # Conservative allowance estimates, distinct from actual billed usage.
        estimated = sum(1536 if item["type"] == "input_image" else max(1, len(item.get("text", "")) // 3)
                        for item in content) + len(str(kwargs.get("questions", []))) // 3 + 300
        if estimated > self.tpm:
            raise ValueError("Shared TPM allowance cannot accommodate one Decisions request")
        with self.lock:
            while True:
                now = self.clock()
                if now < self.expires and self.requests >= 1 and self.tokens >= estimated:
                    self.requests -= 1
                    self.tokens -= estimated
                    return
                self.check_owner()
                delay = self._lease(estimated, now)
                if delay == 0:
                    continue
                self.sleep(min(1, max(.01, delay)))

    def _lease(self, estimate, now):
        from google.cloud import firestore
        count = max(1, min(16, int(self.rpm), int(self.tpm // estimate)))
        tokens = estimate * count

        @firestore.transactional
        def lease(tx):
            snapshot = next(tx.get(self.ref))
            saved = snapshot.to_dict() or {}
            elapsed = max(0, now - saved.get("at", now))
            requests_available = min(self.rpm, saved.get("requests", self.rpm) + elapsed * self.rpm / 60)
            tokens_available = min(self.tpm, saved.get("tokens", self.tpm) + elapsed * self.tpm / 60)
            if requests_available < count or tokens_available < tokens:
                return max((count-requests_available) * 60 / self.rpm, (tokens-tokens_available) * 60 / self.tpm)
            tx.set(self.ref, {"at": now, "requests": requests_available-count, "tokens": tokens_available-tokens})
            return 0
        delay = lease(client().transaction())
        if delay == 0:
            self.requests, self.tokens, self.expires = count, tokens, now + 10
        return delay
