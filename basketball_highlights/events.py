from __future__ import annotations

from .models import Detection


def made_basket_probability(detection: Detection) -> float:
    for answer in detection.api_response.get("answers", []):
        if answer.get("name") == "outcome":
            return max((float(item.get("probability", 0))
                        for item in answer.get("probabilities", [])
                        if item.get("value") == "made_basket"), default=0.0)
    return 0.0


def should_refine(detection: Detection, threshold: float = 0.05) -> bool:
    return detection.classification == "uncertain" and made_basket_probability(detection) >= threshold


def make_windows(duration: float, window_seconds: float = 1.0, stride_seconds: float = 0.5) -> list[tuple[float, float]]:
    if window_seconds <= 0 or stride_seconds <= 0:
        raise ValueError("window and stride must be positive")
    if stride_seconds > window_seconds:
        raise ValueError("stride must not exceed window duration")
    starts = []
    t = 0.0
    while t < duration:
        starts.append((t, min(duration, t + window_seconds)))
        if t + window_seconds >= duration:
            break
        t += stride_seconds
    return starts


def merge_detections(items: list[tuple[str, Detection]], duplicate_seconds: float = 0.75) -> list[dict]:
    """Merge overlapping made/uncertain candidates when they share frames and time."""
    candidates = sorted(((wid, d) for wid, d in items
                         if d.classification == "made_basket" or should_refine(d)),
                        key=lambda pair: pair[1].estimated_event_timestamp or 0)
    groups: list[list[tuple[str, Detection]]] = []
    for pair in candidates:
        ts = pair[1].estimated_event_timestamp or 0
        shares_evidence = bool(groups and set(pair[1].evidence_timestamps).intersection(
            t for _, detection in groups[-1] for t in detection.evidence_timestamps))
        if groups and shares_evidence and ts - (groups[-1][-1][1].estimated_event_timestamp or 0) <= duplicate_seconds:
            groups[-1].append(pair)
        else:
            groups.append([pair])
    merged = []
    for group in groups:
        confirmed = [pair for pair in group if pair[1].classification == "made_basket"]
        strongest = confirmed or group
        best = max(strongest, key=lambda p: p[1].model_confidence if p[1].model_confidence is not None else -1)
        times = sorted({t for _, d in group for t in d.evidence_timestamps})
        classification = "made_basket" if confirmed else "uncertain"
        merged.append({"timestamp": best[1].estimated_event_timestamp, "classification": classification,
            "shot_type": best[1].shot_type, "confidence": best[1].model_confidence,
            "reason": best[1].reason, "evidence_timestamps": times,
            "source_windows": sorted({source for wid, d in group
                                      for source in (d.refinement_sources or [wid])}),
            "needs_review": not bool(confirmed)})
    return sorted(merged, key=lambda event: event["timestamp"] or 0)


def clip_bounds(timestamp: float, duration: float, pre: float, post: float) -> tuple[float, float]:
    return max(0.0, timestamp - pre), min(duration, timestamp + post)
