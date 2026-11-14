"""Bounded frame preparation; no model, budget or journal work on decoder threads."""
from __future__ import annotations


def prepare_jobs(entries, frame, phase, chronological):
    """Return requests in admission order, optionally decoding shared evidence once."""
    samples = {}
    failures = {}
    if chronological and entries and entries[0][0]["kind"] != "base":
        timestamps = sorted({t for job, _, _ in entries for t in job["samples"]})
        for timestamp in timestamps:
            try:
                samples[timestamp] = frame(timestamp, "extra", phase)
            except Exception as exc:
                failures[timestamp] = exc
    prepared = []
    for entry in entries:
        job, _, _ = entry
        try:
            if job["kind"] == "base":
                frames = {key: [frame(t, "base", phase) for t in times]
                          for key, times in job["samples"].items()}
            elif chronological:
                for timestamp in job["samples"]:
                    if timestamp in failures:
                        raise failures[timestamp]
                frames = [samples[t] for t in job["samples"]]
            else:
                frames = [frame(t, "extra", phase) for t in job["samples"]]
            prepared.append((entry, frames, None))
        except Exception as exc:
            prepared.append((entry, None, exc))
    return prepared
