"""Deterministic candidate-level evidence planning; no decoding or API work."""
from __future__ import annotations

import hashlib
import math

from .events import made_basket_probability, should_refine


def settings(args, saved=None):
    # Missing attributes belong to old pinned hosted/local configurations. CLI
    # None values mean "use the new default", or inherit a saved resume setting.
    candidate = getattr(args, "candidate_refinement", False)
    if candidate is None:
        candidate = saved.get("candidate_refinement", False) if saved is not None else getattr(args, "pipeline_version", 1) == 2
    def option(name, legacy, current):
        value = getattr(args, name, legacy)
        if value is None:
            return saved.get(name, legacy) if saved is not None else current
        return value
    config = {
        "candidate_refinement": candidate,
        "refinement_frames": option("refinement_frames", 5, 9 if candidate else 5),
        "rescue_threshold": option("rescue_threshold", .05, .05),
        "rescue_missed": option("rescue_missed", False, False),
        "rescue_max_requests": option("rescue_max_requests", 16, 0),
    }
    if (config["refinement_frames"] not in (5, 9)
            or not math.isfinite(config["rescue_threshold"])
            or not 0 <= config["rescue_threshold"] <= .05
            or not 0 <= config["rescue_max_requests"] <= 256):
        raise ValueError("Invalid refinement settings")
    if not config["candidate_refinement"] and (config["refinement_frames"] != 5
            or config["rescue_threshold"] != .05 or config["rescue_missed"]):
        raise ValueError("Experimental evidence/rescue settings require candidate refinement")
    return config


def candidate_jobs(done, duration, config, frame_rate=None):
    fps = frame_rate if frame_rate and math.isfinite(frame_rate) and frame_rate > 0 else 30
    last_frame = max(0, duration - 1 / fps)
    base = [(key, detection) for key, detection in done.items() if not key.endswith("_refine")]
    eligible = [(key, d) for key, d in base if should_refine(d)]
    rescue = [(key, d) for key, d in base
              if not should_refine(d) and d.classification != "made_basket"
              and (d.classification == "uncertain" or config["rescue_missed"])
              and made_basket_probability(d) >= config["rescue_threshold"]]
    # Anchor each cluster to its first timestamp: shared frames alone could chain
    # an arbitrarily long sequence of distinct attempts together.
    clusters = []
    for key, detection in sorted(eligible + rescue,
                                 key=lambda pair: (pair[1].estimated_event_timestamp or 0, pair[0])):
        timestamp = detection.estimated_event_timestamp or 0
        if (clusters and timestamp - (clusters[-1][0][1].estimated_event_timestamp or 0) <= .5
                and set(detection.evidence_timestamps).intersection(
                    t for _, other in clusters[-1] for t in other.evidence_timestamps)):
            clusters[-1].append((key, detection))
        else:
            clusters.append([(key, detection)])
    jobs = []
    for cluster in clusters:
        members = sorted(key for key, _ in cluster)
        best = max(cluster, key=lambda pair: (made_basket_probability(pair[1]), pair[0]))[1]
        center = best.estimated_event_timestamp or 0
        frames = config["refinement_frames"]
        half_span = .5 if frames == 9 else .25
        samples = sorted({min(last_frame, max(0, center - half_span + i * .125))
                          for i in range(frames)})
        key = "candidate_" + hashlib.sha256("|".join(members).encode()).hexdigest()[:24] + "_refine"
        jobs.append({"kind": "refinement", "key": key, "samples": samples, "members": members,
                     "rescue": not any(should_refine(d) for _, d in cluster),
                     "probability": max(made_basket_probability(d) for _, d in cluster)})
    regular = [job for job in jobs if not job["rescue"]]
    extra = sorted((job for job in jobs if job["rescue"]), key=lambda job: (-job["probability"], job["key"]))
    return regular + extra[:config["rescue_max_requests"]]
