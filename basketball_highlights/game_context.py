"""Candidate identities and wider frame sampling for game-context checks."""
from __future__ import annotations

import hashlib
import json
import math

from .models import ContextResult

CONTEXT_CONFIDENCE = 0.90
CONTEXT_OFFSETS = (-10, -6, -3, 0, 3, 6, 10)
ANALYSIS_VERSION = 2


def candidate_key(candidate: dict) -> str:
    windows = json.dumps(sorted(candidate["source_windows"]), separators=(",", ":"))
    return hashlib.sha256(windows.encode()).hexdigest()


def context_times(center: float, duration: float, frame_rate: float | None = None) -> list[float]:
    # Seek no later than the beginning of the final frame, rather than EOF.
    fps = frame_rate if frame_rate and math.isfinite(frame_rate) and frame_rate > 0 else 30.0
    last_frame = max(0.0, duration - 1.0 / fps)
    return sorted({min(last_frame, max(0.0, center + offset)) for offset in CONTEXT_OFFSETS})


def apply_context(candidates: list[dict], results: dict[str, ContextResult]) -> tuple[list[dict], dict]:
    retained = []
    counts = {"checked": 0, "excluded": 0, "unchecked": 0}
    for candidate in candidates:
        result = results.get(candidate_key(candidate), ContextResult())
        counts["checked" if result.checked else "unchecked"] += 1
        definitive = result.checked and result.confidence is not None and result.confidence >= CONTEXT_CONFIDENCE
        context = result.classification if definitive else "uncertain"
        if context == "warmup_or_break":
            counts["excluded"] += 1
            continue
        item = {**candidate, "game_context": context,
                "included": candidate["classification"] == "made_basket" and context == "game_play"}
        if context == "uncertain":
            item["needs_review"] = True
        retained.append(item)
    return retained, counts
