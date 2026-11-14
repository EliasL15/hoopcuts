"""Offline overhead benchmark and explicit, labeled full-game benchmark matrix.

Run from the hosted_highlights repository root. The `overhead` command makes no
API requests. The `games` command requires --execute before it spends money.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basketball_highlights import cli
from basketball_highlights.budget import Budget
from basketball_highlights.evaluate import evaluate, evaluate_accuracy
from basketball_highlights.journal import Journal
from basketball_highlights.models import Detection
from basketball_highlights.video import FrameReader, ffprobe, sample_frame
from basketball_highlights.models import ContextResult


def execution_options(options, profile, decoder_threads=2):
    """Explicit settings: the benchmark must exercise the hosted decoder path."""
    options.timestamp_decoder = True
    options.preview_max_dimension = 720
    options.prefetch_requests = 4 if profile != "current" else 0
    options.rolling_scheduler = profile == "rolling"
    options.chronological_evidence = profile != "current"
    options.decoder_thread_type = "SLICE" if profile == "current" else "AUTO"
    options.decoder_threads = decoder_threads


def accuracy_options(options, profile):
    options.candidate_refinement = profile != "baseline"
    options.refinement_frames = 9 if profile in ("wider", "combined") else 5
    options.rescue_threshold = .02 if profile in ("rescue", "rescue-missed", "combined") else .05
    options.rescue_missed = profile in ("rescue-missed", "combined")
    options.rescue_max_requests = 16


def scheduling(args):
    """Real decoding with fixed model responses. No API calls or quality claims."""
    from basketball_highlights import pipeline
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    baseline = None
    original = pipeline.DecisionsAdapter
    for profile in ("current", "prefetch", "rolling"):
        payloads = []
        class ReplayAdapter:
            last_attempts = 1
            last_request_id = "replay"
            def record(self, frames):
                digest = hashlib.sha256()
                for frame in frames:
                    digest.update(str(frame.timestamp).encode())
                    digest.update(frame.jpeg)
                payloads.append(digest.hexdigest())
                time.sleep(args.model_latency)
            def detection(self, frames):
                center = frames[len(frames)//2].timestamp
                # Exercise refinements, global merging and context evidence.
                outcome = "uncertain" if int(center) % 10 == 4 else "made_basket" if int(center) % 10 == 5 else "missed_shot"
                return Detection(classification=outcome, estimated_event_timestamp=center,
                    evidence_timestamps=[frame.timestamp for frame in frames], model_confidence=.99,
                    api_response={"answers": [{"name": "outcome", "probabilities": [
                        {"value": "made_basket", "probability": .5}]}]})
            def classify(self, frames):
                self.record(frames)
                return self.detection(frames), {"input_tokens": 100}
            def classify_group(self, windows):
                unique = {frame.timestamp: frame for frames in windows.values() for frame in frames}
                self.record([unique[t] for t in sorted(unique)])
                return {key: self.detection(frames) for key, frames in windows.items()}, {"input_tokens": 100}
            def classify_context(self, frames, timestamp):
                self.record(frames)
                return ContextResult(classification="game_play", confidence=.99, checked=True), {"input_tokens": 100}
        options = cli.parser().parse_args(["analyze", str(args.video), "--output-dir", str(args.output/profile),
            "--group-windows", "8", "--csv-only", "--max-concurrency", "4", "--limit-seconds", str(args.limit_seconds)])
        execution_options(options, profile, args.decoder_threads)
        pipeline.DecisionsAdapter = ReplayAdapter
        try:
            cli.analyze(options)
        finally:
            pipeline.DecisionsAdapter = original
        budget = json.loads((options.output_dir/".state"/"budget.json").read_text())
        # Reservation UUIDs and completion order are intentionally excluded.
        parity = {"events": (options.output_dir/"events.csv").read_text(), "payloads": sorted(payloads),
                  "usage": {key: budget[key] for key in ("input_tokens", "image_count", "request_count")},
                  "spend": round(budget["estimated_spend_usd"], 8)}
        if baseline is None:
            baseline = parity
        assert parity == baseline, f"Scheduling changed evidence/results/accounting: {profile}"
        report = json.loads((options.output_dir/".state"/"performance.json").read_text())
        row = {"profile": profile, "parity_passed": True, "performance": report}
        rows.append(row)
        print(json.dumps({"profile": profile, "seconds": report["elapsed_seconds"], "parity_passed": True}), flush=True)
    (args.output/"report.json").write_text(json.dumps({"api_requests": 0, "rows": rows}, indent=2))


def overhead(output, requests):
    output.mkdir(parents=True, exist_ok=False)
    report = {"api_requests": 0, "synthetic_windows": requests, "persistence": {}, "decoding": {}}
    for version in (1, 2):
        folder = output / f"v{version}"
        folder.mkdir()
        journal = Journal(folder / "analysis.sqlite3") if version == 2 else None
        budget = Budget(folder / "budget.json", 5, journal=journal)
        signature = {"duration": requests / 4, "window_seconds": .75, "stride_seconds": .25}
        state = {"signature": signature, "detections": {}, "contexts": {}, "errors": []}
        checkpoint_bytes = 0
        if journal:
            journal.set_meta("signature", signature)
        start, half = time.monotonic(), None
        for number in range(requests):
            key = f"w{number:06d}"
            value = Detection(classification="uncertain", estimated_event_timestamp=number / 4,
                evidence_timestamps=[number / 4, (number+1)/4, (number+2)/4],
                api_response={"answers": [{"name": "outcome", "type": "choice", "choice": "uncertain", "confidence": .8,
                    "probabilities": [{"value": "made_basket", "probability": .01}, {"value": "uncertain", "probability": .99}]}]}).model_dump()
            reserve = budget.reserve(3, attempts=4)
            if journal:
                with journal.transaction():
                    budget.settle(reserve, input_tokens=1116, output_tokens=0, images=3, request_id=key)
                    journal.put("detections", key, value)
            else:
                budget.settle(reserve, input_tokens=1116, output_tokens=0, images=3, request_id=key)
                state["detections"][key] = value
                (folder / "run_state.json").write_text(json.dumps(state))
            if (number+1) % 32 == 0:
                if journal:
                    latest_state, latest_budget = journal.checkpoint(signature, [], number+1, compact=(number+1) % 256 == 0 or number == 31)
                    body = gzip.compress(json.dumps({"state": latest_state, "budget": latest_budget}).encode())
                    journal.published()
                else:
                    body = gzip.compress(json.dumps({"state": state, "budget": budget.data}).encode())
                checkpoint_bytes += len(body)
                (folder / "checkpoint.gz").write_bytes(body)
            if number+1 == requests // 2:
                half = time.monotonic()
        end = time.monotonic()
        if journal:
            journal.export(folder)
            journal.close()
        report["persistence"][f"v{version}"] = {"seconds": end-start, "first_half_seconds": half-start,
            "second_half_seconds": end-half, "checkpoint_bytes": checkpoint_bytes}
    video = output / "decode.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=30",
                    "-c:v", "libx264", "-preset", "ultrafast", str(video)], check=True)
    for version in (1, 2):
        reader = FrameReader(video, sequential=True, info=ffprobe(video)) if version == 2 else None
        started = time.monotonic()
        frames = [(reader.sample(number/4) if reader else sample_frame(video, number/4)).jpeg for number in range(120)]
        report["decoding"][f"v{version}"] = {"seconds": time.monotonic()-started, "frames": len(frames)}
        if reader:
            reader.close()
        if version == 1:
            expected = frames
        else:
            report["decoding"]["identical_jpegs"] = frames == expected
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


def match_labels(predicted, truth, tolerance=3):
    remaining = set(truth)
    matched = set()
    duplicates = 0
    for timestamp in sorted(predicted):
        if remaining:
            target = min(remaining, key=lambda value: abs(value-timestamp))
            if abs(target-timestamp) <= tolerance:
                matched.add(target)
                remaining.remove(target)
                continue
        if any(abs(target-timestamp) <= tolerance for target in matched):
            duplicates += 1
    return matched, duplicates


def summarize_accuracy(rows, recordings):
    """A perfect baseline game need not improve; require a gain across the split."""
    baselines = {}
    for row in rows:
        if row.get("complete") and row["game"] not in baselines:
            baselines[row["game"]] = row
    summaries = []
    identities = sorted({(r["group_windows"], r["concurrency"], r["execution_profile"], r["accuracy_profile"]) for r in rows})
    for group, concurrency, execution, accuracy in identities:
        for split in ("tuning", "validation"):
            expected = {game["id"] for game in recordings if game["split"] == split}
            selected = [r for r in rows if r["game"] in expected and
                        (r["group_windows"], r["concurrency"], r["execution_profile"], r["accuracy_profile"]) ==
                        (group, concurrency, execution, accuracy)]
            complete = len(selected) == len(expected) and all(r.get("complete") for r in selected)
            usable = [r for r in selected if r.get("complete")]
            gained = sum(r["accuracy"]["automatically_selected"]["matched"] -
                         baselines[r["game"]]["accuracy"]["automatically_selected"]["matched"] for r in usable)
            quality = complete and all(r["quality_passed"] for r in usable)
            time_passed = complete and all(r["time_passed"] for r in usable)
            summaries.append({"group_windows": group, "concurrency": concurrency,
                "execution_profile": execution, "accuracy_profile": accuracy, "split": split,
                "complete": complete, "additional_selected_baskets": gained,
                "quality_passed": quality, "time_passed": time_passed,
                "accepted": quality and time_passed and gained > 0})
    return summaries


def games(args):
    manifest = json.loads(args.manifest.read_text())
    recordings = manifest["games"]
    if manifest.get("label_source") != "human":
        raise ValueError("Manifest must declare human labels; model-generated labels are not ground truth")
    if not {"tuning", "validation"}.issubset({game["split"] for game in recordings}):
        raise ValueError("Provide separate tuning and validation games")
    if len(recordings) < 3:
        raise ValueError("Provide at least three full games")
    identities = set()
    videos = set()
    for game in recordings:
        if game["split"] not in {"tuning", "validation"}:
            raise ValueError("Game split must be tuning or validation")
        if game["id"] in identities:
            raise ValueError("Game IDs must be unique")
        identities.add(game["id"])
        for field in ("video", "labels_csv"):
            game[field] = str((args.manifest.parent / game[field]).resolve())
            if not Path(game[field]).is_file():
                raise FileNotFoundError(game[field])
        if game["video"] in videos:
            raise ValueError("Use distinct recordings for each game; tuning and validation must not overlap")
        videos.add(game["video"])
    cases = [(2, group, concurrency, profile, accuracy) for profile in args.profiles
             for group in args.groups for concurrency in args.concurrencies for accuracy in args.accuracy_profiles]
    if not args.execute:
        print(json.dumps({"api_requests": 0, "planned_analyses": len(cases)*len(recordings),
            "cases": cases, "resource_profile": [args.cpu, args.memory_gib],
            "note": "Use --execute only after reviewing the API spend for this matrix."}, indent=2))
        return
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for game_number, game in enumerate(recordings):
        # Build game-only truth; context evaluation still uses the original labels.
        with open(game["labels_csv"], newline="") as stream:
            labels = list(csv.DictReader(stream))
        truth = [float(row["timestamp_seconds"]) for row in labels if row.get("game_context", "game_play") == "game_play"]
        truth_csv = args.output / f"truth-{game_number}.csv"
        with truth_csv.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["timestamp_seconds"])
            writer.writerows([[timestamp] for timestamp in truth])
        baseline_matches, baseline_precision, baseline_duplicates = None, None, None
        baseline_selected = None
        baseline_elapsed = baseline_selected_precision = None
        for version, group, concurrency, profile, accuracy in cases:
            folder = args.output / f"game-{game_number}-v{version}-g{group}-c{concurrency}-{profile}-{accuracy}"
            options = cli.parser().parse_args(["analyze", game["video"], "--output-dir", str(folder),
                "--pipeline-version", str(version), "--group-windows", str(group), "--max-concurrency", str(concurrency),
                "--budget-usd", str(args.budget_usd)])
            options.create_reel_output = False
            execution_options(options, profile, args.decoder_threads)
            accuracy_options(options, accuracy)
            started = time.monotonic()
            try:
                cli.analyze(options)
                elapsed = time.monotonic()-started
                events = json.loads((folder / "events.json").read_text())
                predictions = [event["timestamp_seconds"] for event in events if event["classification"] == "made_basket"]
                metrics = evaluate(predictions, str(truth_csv))
                matched, duplicates = match_labels(predictions, truth)
                selected = [event["timestamp_seconds"] for event in events if event["included"]]
                selected_matches, _ = match_labels(selected, truth)
                stage_metrics = evaluate_accuracy(json.loads((folder / "run_state.json").read_text()),
                                                  events, game["labels_csv"])
                if baseline_matches is None:
                    baseline_matches, baseline_precision, baseline_duplicates = matched, metrics["precision"], duplicates
                    baseline_selected = selected_matches
                    baseline_elapsed = elapsed
                    baseline_selected_precision = stage_metrics["automatically_selected"]["precision"]
                summary = json.loads((folder / "processing_summary.json").read_text())
                spend = summary["budget"]["estimated_spend_usd"]
                duration = summary["video"]["duration_seconds"]
                row = {"game": game["id"], "split": game["split"], "camera_style": game["camera_style"],
                    "pipeline_version": version, "group_windows": group, "concurrency": concurrency,
                    "execution_profile": profile, "timestamp_decoder": True, "decoder_threads": args.decoder_threads,
                    "accuracy_profile": accuracy, "accuracy": stage_metrics,
                    "performance": summary.get("performance"),
                    "elapsed_seconds": elapsed, "model_spend_usd": spend, "model_usd_per_footage_hour": spend*3600/duration,
                    "estimated_compute_usd": elapsed*(args.cpu*.000018+args.memory_gib*.000002),
                    "additional_missed_labels": sorted(baseline_matches-matched), **metrics,
                    "duplicate_events": duplicates,
                    "additional_unselected_labels": sorted(baseline_selected-selected_matches),
                    "quality_passed": (baseline_matches.issubset(matched) and baseline_selected.issubset(selected_matches)
                                       and metrics["precision"] >= baseline_precision and duplicates <= baseline_duplicates
                                       and stage_metrics["automatically_selected"]["precision"] >= baseline_selected_precision),
                    "recall_improved": len(selected_matches) > len(baseline_selected),
                    "elapsed_ratio": elapsed / baseline_elapsed,
                    "time_passed": elapsed <= baseline_elapsed * 1.10, "complete": summary["complete"]}
                row["accepted"] = row["quality_passed"] and row["recall_improved"] and row["time_passed"] and row["complete"]
            except Exception as exc:
                row = {"game": game["id"], "pipeline_version": version, "group_windows": group,
                       "concurrency": concurrency, "execution_profile": profile, "accuracy_profile": accuracy,
                       "error": type(exc).__name__, "complete": False}
                rows.append(row)
                (args.output / "matrix.json").write_text(json.dumps(rows, indent=2))
                # A failed baseline cannot support quality comparisons.
                if baseline_matches is None:
                    raise
                continue
            rows.append(row)
            (args.output / "matrix.json").write_text(json.dumps(rows, indent=2))
    print(json.dumps(rows, indent=2))
    (args.output / "accuracy-summary.json").write_text(json.dumps(summarize_accuracy(rows, recordings), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("overhead")
    offline.add_argument("--output", type=Path, required=True)
    offline.add_argument("--requests", type=int, default=1024)
    full = sub.add_parser("games")
    full.add_argument("manifest", type=Path)
    full.add_argument("--output", type=Path, required=True)
    full.add_argument("--execute", action="store_true")
    full.add_argument("--cpu", type=float, default=2)
    full.add_argument("--memory-gib", type=float, default=8)
    full.add_argument("--budget-usd", type=float, default=5)
    full.add_argument("--profiles", nargs="+", choices=("hosted", "current", "prefetch", "rolling"), default=["hosted"])
    full.add_argument("--accuracy-profiles", nargs="+", choices=("baseline", "candidates", "wider", "rescue", "rescue-missed", "combined"),
                      default=["baseline"])
    full.add_argument("--groups", nargs="+", type=int, choices=(1, 4, 8), default=[8])
    full.add_argument("--concurrencies", nargs="+", type=int, choices=(4, 8, 16), default=[4])
    full.add_argument("--decoder-threads", type=int, choices=(2, 4), default=2)
    replay = sub.add_parser("scheduling", help="Real video + fixed model responses; no API calls")
    replay.add_argument("video", type=Path)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--limit-seconds", type=float, default=64)
    replay.add_argument("--model-latency", type=float, default=.5)
    replay.add_argument("--decoder-threads", type=int, choices=(2, 4), default=2)
    args = parser.parse_args()
    if args.command == "overhead":
        if args.requests < 64 or args.requests % 32:
            parser.error("--requests must be a multiple of 32, at least 64")
        overhead(args.output, args.requests)
    elif args.command == "scheduling":
        if args.limit_seconds <= 0 or args.model_latency < 0:
            parser.error("Use positive duration and nonnegative latency")
        scheduling(args)
    else:
        if args.profiles[0] not in ("current", "hosted") or args.accuracy_profiles[0] != "baseline":
            parser.error("Start with a current/hosted execution profile and baseline accuracy profile")
        games(args)


if __name__ == "__main__":
    main()
