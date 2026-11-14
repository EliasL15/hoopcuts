"""Compare old seeking and timestamp-forward decoding locally without API calls."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from basketball_highlights.video import FrameReader, ffprobe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frames", type=int, default=256)
    args = parser.parse_args()
    info = ffprobe(args.video)
    assert info["variable_frame_rate"] and info["rotation"] == 0, "Use unrotated variable-rate footage"
    assert 1 <= args.frames <= 1024
    span = args.frames / 4
    starts = (0, max(0, (info["duration_seconds"] - span) / 2), max(0, info["duration_seconds"] - span))
    report = {"api_requests": 0, "samples_per_second": 4, "frames_per_section": args.frames, "sections": []}
    for start in starts:
        timings = {}
        baseline = None
        for label, timestamp_aware, thread_type, threads in (
                ("old_seek", False, "SLICE", 2), ("timestamp_forward", True, "SLICE", 2),
                ("auto_2", True, "AUTO", 2), ("auto_4", True, "AUTO", 4)):
            # Include opening and first keyframe seek in both timings.
            tick = time.monotonic()
            reader = FrameReader(args.video, info=info, timestamp_aware=timestamp_aware,
                                 decoder_thread_type=thread_type, decoder_threads=threads)
            evidence = []
            try:
                for number in range(args.frames):
                    sampled = reader.sample(start + number / 4)
                    if timestamp_aware:
                        evidence.append((reader.timestamp_reader.selected_time, sampled.jpeg))
            finally:
                reader.close()
            timings[label] = time.monotonic() - tick
            if label == "timestamp_forward":
                baseline = evidence
            elif timestamp_aware:
                assert evidence == baseline, f"Threading changed PTS/JPEG evidence: {label} at {start}"
        reader = FrameReader(args.video, info=info)
        try:
            sampled = reader.sample(start)
            selected = reader.timestamp_reader.selected_time
        finally:
            reader.close()
        # FFmpeg seeks on the edited movie timeline; decode exactly the selected
        # frame without average-FPS indexing as an independent pixel reference.
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", f"{max(0, selected - .00001):.8f}",
            "-i", str(args.video), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"])
        reference = np.frombuffer(raw, np.uint8).reshape(info["height"], info["width"], 3)
        scale = min(1, 768 / max(info["height"], info["width"]))
        reference = cv2.resize(reference, (round(info["width"] * scale), round(info["height"] * scale)), interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode(".jpg", reference, [cv2.IMWRITE_JPEG_QUALITY, 85])
        assert ok
        reference = cv2.imdecode(jpeg, cv2.IMREAD_COLOR)
        actual = cv2.imdecode(np.frombuffer(sampled.jpeg, np.uint8), cv2.IMREAD_COLOR)
        error = float(np.abs(actual.astype(float) - reference).mean())
        # Different FFmpeg builds round YUV-to-BGR slightly differently (up to
        # three channel levels on the real broadcast); allow that, not wrong frames.
        assert error < 3, f"Reference frame differs at {start}: {error}"
        row = {"start_seconds": start, "seconds": timings,
            "speedup": timings["old_seek"] / timings["timestamp_forward"],
            "auto_2_speedup": timings["timestamp_forward"] / timings["auto_2"],
            "auto_4_speedup": timings["timestamp_forward"] / timings["auto_4"],
            "threading_evidence_identical": True,
            "selected_pts_seconds": selected, "reference_mean_pixel_error": error}
        report["sections"].append(row)
        print(json.dumps(row), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
