"""Generate clips and a reel from an existing events CSV, without API calls."""
from __future__ import annotations

import csv
from pathlib import Path

from .clips import create_clip, create_reel
from .video import ffprobe


def generate_from_csv(video: Path, events_csv: Path, output: Path) -> list[Path]:
    """Honor explicit selection, or legacy confirmed/review-required CSV rows."""
    if not video.is_file():
        raise FileNotFoundError(f"Video file not found: {video}")
    if not events_csv.is_file():
        raise FileNotFoundError(f"Events CSV not found: {events_csv}")
    duration = ffprobe(video)["duration_seconds"]
    output.mkdir(parents=True, exist_ok=True)
    clips_dir = output / "clips"
    clip_paths: list[Path] = []
    with events_csv.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if "included" in row:
                if str(row["included"]).strip().lower() not in {"true", "1", "yes"}:
                    continue
            else:
                classification = row.get("classification", "")
                needs_review = str(row.get("needs_review", "")).strip().lower() in {"true", "1", "yes"}
                if classification != "made_basket" and not needs_review:
                    continue
            timestamp = float(row["timestamp_seconds"])
            start = max(0.0, float(row.get("clip_start_seconds") or timestamp - 5))
            end = min(duration, float(row.get("clip_end_seconds") or timestamp + 5))
            if end <= start:
                raise ValueError(f"Invalid clip bounds for event at {timestamp:.3f}s")
            target = clips_dir / f"basket_{len(clip_paths)+1:03d}.mp4"
            create_clip(video, target, start, end)
            clip_paths.append(target)
    if clip_paths:
        create_reel(clip_paths, output / "highlights_reel.mp4")
    return clip_paths
