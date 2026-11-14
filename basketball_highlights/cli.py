from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import queue
import sys
import threading
import time
from typing import Callable
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from dotenv import load_dotenv

from .budget import Budget, BudgetExceeded
from .clips import create_clip, create_reel
from .csv_highlights import generate_from_csv
from .decisions import DecisionsAdapter, DecisionsUnavailable
from .retry import RequestGate, RequestFailed
from .events import clip_bounds, make_windows, merge_detections, should_refine
from .evaluate import evaluate as evaluate_events, evaluate_context, evaluate_accuracy
from .game_context import ANALYSIS_VERSION, apply_context, candidate_key, context_times
from .models import ContextResult, Detection, Event
from .video import ffprobe, sample_frame, sample_times

log = logging.getLogger("basketball_highlights")
MAX_REQUEST_CONCURRENCY = 16


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m basketball_highlights")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("analyze", help="Analyze a game and create highlight clips")
    a.add_argument("video", type=Path)
    a.add_argument("--output-dir", type=Path)
    a.add_argument("--sampling-fps", type=float, default=4.0)
    a.add_argument("--window-seconds", type=float, default=0.75)
    a.add_argument("--stride-seconds", type=float, default=0.25)
    a.add_argument("--max-image-dimension", type=int, default=768)
    a.add_argument("--jpeg-quality", type=int, default=85)
    a.add_argument("--pre-padding", type=float, default=5)
    a.add_argument("--post-padding", type=float, default=5)
    a.add_argument("--budget-usd", type=float, default=5.0)
    a.add_argument("--max-concurrency", type=int, default=4,
                   help="Maximum simultaneous Decisions requests (1-16; default: 4)")
    a.add_argument("--pipeline-version", type=int, choices=(1, 2), default=2)
    a.add_argument("--timestamp-decoder", action=argparse.BooleanOptionalAction, default=False)
    a.add_argument("--prefetch-requests", type=int, default=0, help="Bounded frame preparation ahead of dispatch (0-16)")
    a.add_argument("--rolling-scheduler", action=argparse.BooleanOptionalAction, default=False)
    a.add_argument("--chronological-evidence", action=argparse.BooleanOptionalAction, default=False)
    a.add_argument("--decoder-thread-type", choices=("SLICE", "AUTO", "FRAME"), default="SLICE")
    a.add_argument("--decoder-threads", type=int, choices=(1, 2, 4), default=2)
    a.add_argument("--preview-max-dimension", type=int, choices=(720,))
    a.add_argument("--group-windows", type=int, choices=(1, 4, 8), default=1,
                   help="Experimental shared-input grouping; keep 1 until human-label validation passes")
    a.add_argument("--candidate-refinement", action=argparse.BooleanOptionalAction, default=None,
                   help="Consolidate candidate checks (default: on for new pipeline v2 runs)")
    a.add_argument("--refinement-frames", type=int, choices=(5, 9), default=None,
                   help="Refinement evidence frames (default: 9 with candidate refinement; 5 otherwise)")
    a.add_argument("--rescue-threshold", type=float, default=None)
    a.add_argument("--rescue-missed", action=argparse.BooleanOptionalAction, default=None)
    a.add_argument("--rescue-max-requests", type=int, default=None,
                   help="Maximum additional rescue candidates per run (0-256; default: 0)")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--resume", action="store_true")
    a.add_argument("--csv-only", action="store_true",
                   help="Write only events.csv; keep budget/resume state in a hidden .state folder")
    a.add_argument("--limit-seconds", type=float)
    a.add_argument("--verbose", action="store_true")
    e = sub.add_parser("evaluate", help="Compare detected events with a labeled CSV")
    e.add_argument("events_json", type=Path)
    e.add_argument("labels_csv", type=Path)
    e.add_argument("--tolerance-seconds", type=float, default=3.0)
    e.add_argument("--verbose", action="store_true")
    c = sub.add_parser("evaluate-context", help="Compare saved context checks with human-labeled baskets")
    c.add_argument("run_state", type=Path)
    c.add_argument("labels_csv", type=Path)
    c.add_argument("--tolerance-seconds", type=float, default=3.0)
    c.add_argument("--verbose", action="store_true")
    accuracy = sub.add_parser("evaluate-accuracy", help="Evaluate detection, context and automatic-selection recall")
    accuracy.add_argument("run_state", type=Path)
    accuracy.add_argument("events_json", type=Path)
    accuracy.add_argument("labels_csv", type=Path)
    accuracy.add_argument("--tolerance-seconds", type=float, default=3.0)
    accuracy.add_argument("--verbose", action="store_true")
    h = sub.add_parser("highlights", help="Generate clips and a reel from an events CSV")
    h.add_argument("video", type=Path)
    h.add_argument("events_csv", type=Path)
    h.add_argument("--output-dir", type=Path, default=Path("outputs") / "highlights-from-csv")
    h.add_argument("--verbose", action="store_true")
    u = sub.add_parser("ui", help="Launch the local browser review app")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--projects-dir", type=Path, default=Path("outputs") / "ui_projects")
    u.add_argument("--verbose", action="store_true")
    return p


def write_results(output: Path, info: dict, budget: Budget, detections: list[tuple[str, Detection]],
                  errors: list[str], started: float, duration: float, pre: float, post: float,
                  source: Path, create_media: bool = True, complete: bool = False,
                  csv_only: bool = False, create_reel_output: bool = True,
                  context_results: dict[str, ContextResult] | None = None,
                  media_workers: int = 1, preview_max_dimension: int | None = None,
                  media_progress=None, media_ready=None) -> list[Event]:
    combined = merge_detections(detections)
    context_summary = None
    if context_results is not None:
        combined, context_summary = apply_context(combined, context_results)
    events: list[Event] = []
    clip_paths = []
    for index, item in enumerate(combined, 1):
        ts = item["timestamp"]
        if ts is None:
            continue
        start, end = clip_bounds(ts, duration, pre, post)
        event = Event(event_id=f"basket_{index:03d}", timestamp_seconds=ts,
            clip_start_seconds=start, clip_end_seconds=end,
            classification=item["classification"], shot_type=item["shot_type"],
            model_confidence=item["confidence"], needs_review=item["needs_review"],
            evidence_timestamps=item["evidence_timestamps"], reason=item["reason"],
            source_windows=item["source_windows"],
            included=item.get("included", item["classification"] == "made_basket"),
            game_context=item.get("game_context"))
        if create_media and media_workers == 1:
            clip = output / "clips" / f"{event.event_id}.mp4"
            try:
                create_clip(source, clip, start, end)
                event.clip_path = str(clip.relative_to(output))
                clip_paths.append(clip)
            except Exception as exc:
                errors.append(f"clip {event.event_id}: {exc}")
        events.append(event)
    def clip_progress():
        if media_progress:
            media_progress(clips_completed=sum(bool(event.clip_path) for event in events), clips_total=len(events))
    media_error = None
    if create_media and media_workers > 1:
        try:
            clip_progress()
        except BaseException as exc:
            media_error = exc
        def render(event):
            clip = output / "clips" / f"{event.event_id}.mp4"
            create_clip(source, clip, event.clip_start_seconds, event.clip_end_seconds,
                        max_dimension=preview_max_dimension, threads=1)
            return str(clip.relative_to(output))
        with ThreadPoolExecutor(max_workers=min(2, media_workers), thread_name_prefix="preview") as executor:
            remaining = iter(events)
            pending = {}
            def replenish():
                while len(pending) < min(2, media_workers):
                    event = next(remaining, None)
                    if event is None:
                        break
                    pending[executor.submit(render, event)] = event
            if media_error is None:
                replenish()
            while pending:
                completed, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for future in completed:
                    event = pending.pop(future)
                    try:
                        event.clip_path = future.result()
                    except Exception as exc:
                        errors.append(f"clip {event.event_id}: {exc}")
                    if event.clip_path and media_ready and media_error is None:
                        try:
                            clip_progress()
                            media_ready(event, output / event.clip_path)
                        except BaseException as exc:
                            media_error = exc
                if media_progress and media_error is None:
                    try:
                        clip_progress()
                    except BaseException as exc:
                        media_error = exc
                        for future in list(pending):
                            if future.cancel():
                                pending.pop(future)
                if media_error is None:
                    replenish()
            if media_error is None:
                clip_progress()
    if not csv_only:
        (output / "events.json").write_text(json.dumps([e.model_dump() for e in events], indent=2))
    with (output / "events.csv").open("w", newline="", encoding="utf-8") as f:
        cols = list(Event.model_fields.keys())
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for event in events:
            row = event.model_dump()
            row["evidence_timestamps"] = json.dumps(row["evidence_timestamps"])
            row["source_windows"] = json.dumps(row["source_windows"])
            writer.writerow(row)
    if create_media and create_reel_output:
        try:
            selected = [output / event.clip_path for event in sorted(events, key=lambda e: e.timestamp_seconds)
                        if event.included and event.clip_path]
            if selected:
                create_reel(selected, output / "highlights_reel.mp4")
        except Exception as exc:
            errors.append(f"highlights reel: {exc}")
    if csv_only:
        return events
    summary = {
        "video": info, "elapsed_seconds": round(time.monotonic() - started, 2),
        "window_results": len(detections), "made_baskets": sum(e.classification == "made_basket" for e in events),
        "uncertain_events": sum(e.classification == "uncertain" for e in events),
        "budget": {**budget.data, "remaining_usd": budget.remaining},
        "errors": errors, "complete": complete,
        "game_context": context_summary,
        "limitations": ["Decisions API does not return free-form explanations; reason is null.",
                        "Event timestamp is the middle evidence frame unless the model supplies a timestamp (Decisions does not)."],
    }
    (output / "processing_summary.json").write_text(json.dumps(summary, indent=2))
    if media_error is not None:
        raise media_error
    return events


def analyze(args: argparse.Namespace, progress_callback: Callable[[dict], None] | None = None,
            checkpoint_callback: Callable[[dict, dict], None] | None = None,
            source_identity: dict | None = None, request_allowance=None) -> int:
    load_dotenv()
    version = getattr(args, "pipeline_version", 1)
    if version == 2:
        from .pipeline import analyze as analyze_incremental
        return analyze_incremental(args, progress_callback, checkpoint_callback, source_identity, request_allowance)
    if version != 1 or getattr(args, "group_windows", 1) != 1:
        raise ValueError("Unsupported pipeline version or grouping on legacy pipeline")
    from .refinement import settings as refinement_settings
    if refinement_settings(args)["candidate_refinement"]:
        raise ValueError("Candidate refinement requires pipeline version 2")
    return analyze_legacy(args, progress_callback, checkpoint_callback, source_identity)


def analyze_legacy(args: argparse.Namespace, progress_callback: Callable[[dict], None] | None = None,
            checkpoint_callback: Callable[[dict, dict], None] | None = None,
            source_identity: dict | None = None) -> int:
    load_dotenv()
    if not args.video.is_file():
        raise FileNotFoundError(f"Video file not found: {args.video}")
    output = args.output_dir or Path("outputs") / args.video.stem
    output.mkdir(parents=True, exist_ok=True)
    state_dir = output / ".state" if args.csv_only else output
    state_dir.mkdir(parents=True, exist_ok=True)
    if not args.csv_only:
        (output / "debug").mkdir(exist_ok=True)
    info = ffprobe(args.video)
    duration = min(info["duration_seconds"], args.limit_seconds) if args.limit_seconds else info["duration_seconds"]
    info["analyzed_duration_seconds"] = duration
    times = sample_times(duration, args.sampling_fps)
    windows = make_windows(duration, args.window_seconds, args.stride_seconds)
    print(f"Video: {info['width']}x{info['height']} {info['frame_rate']} fps, {duration:.1f}s")
    planned_tokens = len(windows) * (300 + 3 * 400) * 4
    planned_usd = planned_tokens * 0.10 / 1_000_000
    print(f"Sampling: {len(times)} frames; {len(windows)} overlapping windows; "
          f"base-pass request reserve ~{planned_tokens:,} input tokens / ${planned_usd:.4f} "
          "(includes up to four attempts; uncertain-event refinements add usage)")
    max_concurrency = getattr(args, "max_concurrency", 1)
    if not 1 <= max_concurrency <= MAX_REQUEST_CONCURRENCY:
        raise ValueError(f"max-concurrency must be between 1 and {MAX_REQUEST_CONCURRENCY}")
    if args.dry_run:
        print(f"Dry run: no API calls. Output: {output}")
        return 0
    signature = {"analysis_version": ANALYSIS_VERSION,
                 "video": str(args.video.resolve()), "size": args.video.stat().st_size,
                 "mtime_ns": args.video.stat().st_mtime_ns, "duration": duration,
                 "sampling_fps": args.sampling_fps, "window_seconds": args.window_seconds,
                 "stride_seconds": args.stride_seconds, "max_image_dimension": args.max_image_dimension}
    if source_identity is not None:
        signature = {**signature, "source": source_identity}
        for field in ("video", "size", "mtime_ns"):
            signature.pop(field)
        signature.update(jpeg_quality=args.jpeg_quality, pre_padding=args.pre_padding,
                         post_padding=args.post_padding, budget_usd=args.budget_usd)
    run_state_path = state_dir / "run_state.json"
    contexts: dict[str, ContextResult] = {}
    if args.resume and run_state_path.exists():
        state = json.loads(run_state_path.read_text())
        if state.get("signature") != signature:
            raise ValueError("Resume settings, source video or analysis version differ from the saved run; start a new analysis")
        done = {key: Detection.model_validate(value) for key, value in state.get("detections", {}).items()}
        contexts = {key: ContextResult.model_validate(value) for key, value in state.get("contexts", {}).items()}
        errors = state.get("errors", [])
    else:
        done, errors = {}, []
    try:
        adapters = [DecisionsAdapter() for _ in range(max_concurrency)]
    except DecisionsUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    gate = RequestGate(max_concurrency, on_wait=(lambda: progress_callback({
        "phase": "analyzing", "completed": len(completed_keys), "total": len(windows),
        "spend_usd": budget.incurred_spend, "remaining_usd": budget.remaining})) if progress_callback else None)
    for request_adapter in adapters:
        request_adapter.gate = gate
        request_adapter.retries = 3
    adapter = adapters[0]
    budget = Budget(state_dir / "budget.json", args.budget_usd)
    start_time = time.monotonic()
    frame_cache: OrderedDict[float, object] = OrderedDict()
    def get_frame(t: float):
        if t not in frame_cache:
            frame_cache[t] = sample_frame(args.video, t, args.max_image_dimension, args.jpeg_quality)
            while len(frame_cache) > 8:
                frame_cache.popitem(last=False)
        else:
            frame_cache.move_to_end(t)
        return frame_cache[t]
    completed_keys = {key for key in done if not key.endswith("_refine")}
    analysis_finished = False
    # Frame schedule is anchored to the original timeline; each window takes up to three
    # evenly spaced samples while overlapping windows share encoded frame objects.
    try:
        adapter_queue: queue.Queue[DecisionsAdapter] = queue.Queue()
        for worker_adapter in adapters:
            adapter_queue.put(worker_adapter)
        worker_state = threading.local()

        def initialize_worker() -> None:
            worker_state.adapter = adapter_queue.get_nowait()

        def classify_window(frames):
            worker_adapter = worker_state.adapter
            try:
                detection, usage = worker_adapter.classify(frames)
                return detection, usage, max(1, worker_adapter.last_attempts), worker_adapter.last_request_id, None
            except Exception as exc:
                return None, {}, max(0, worker_adapter.last_attempts), worker_adapter.last_request_id, exc

        def save_checkpoint(durable: bool = True) -> None:
            state = {"signature": signature,
                     "detections": {key: done[key].model_dump() for key in sorted(done)},
                     "contexts": {key: contexts[key].model_dump() for key in sorted(contexts)},
                     "errors": errors}
            temp = run_state_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(state))
            temp.replace(run_state_path)
            if durable and checkpoint_callback:
                checkpoint_callback(state, json.loads(budget.path.read_text()))

        completed_keys = {key for key in done if not key.endswith("_refine")}
        in_flight = {}
        next_number = 0
        base_error = None
        save_checkpoint()
        with ThreadPoolExecutor(max_workers=max_concurrency, initializer=initialize_worker,
                                thread_name_prefix="basketball-window") as executor:
            while next_number < len(windows) and base_error is None:
                # Publish every batch's maximum possible spend BEFORE any requests.
                # No next batch may start until these results are durable.
                batch = []
                while next_number < len(windows) and len(batch) < 32:
                    number = next_number
                    next_number += 1
                    key = f"w{number:06d}"
                    if key in done:
                        continue
                    left, right = windows[number]
                    samples = [t for t in times if left <= t < right] or [left]
                    if len(samples) > 3:
                        samples = [samples[round(i * (len(samples)-1) / 2)] for i in range(3)]
                    try:
                        reservation = budget.reserve(len(samples), attempts=adapter.retries + 1)
                    except BudgetExceeded as exc:
                        # Settling the current batch may free conservative reserves.
                        next_number -= 1
                        if not batch:
                            base_error = exc
                            errors.append(str(exc))
                        break
                    batch.append((key, reservation, samples))
                save_checkpoint()
                batch_started = time.monotonic()
                try:
                    while batch or in_flight:
                        while batch and len(in_flight) < gate.concurrency:
                            if base_error is not None:
                                break
                            if time.monotonic() - batch_started >= 30:
                                break
                            key, reservation, samples = batch.pop(0)
                            try:
                                frames = [get_frame(t) for t in samples]
                            except Exception as exc:
                                budget.release(reservation)
                                base_error = exc
                                errors.append(f"{key}: frame decoding failed")
                                break
                            future = executor.submit(classify_window, frames)
                            in_flight[future] = (key, reservation, len(frames))
                        if not in_flight:
                            break
                        finished, _ = wait(in_flight, timeout=1, return_when=FIRST_COMPLETED)
                        for future in finished:
                            key, reservation, image_count = in_flight.pop(future)
                            detection, usage, attempts, request_id, exc = future.result()
                            error_text = f"{key}: {type(exc).__name__}: {exc}" if exc else None
                            if exc:
                                errors.append(error_text)
                                if base_error is None or isinstance(base_error, BudgetExceeded):
                                    base_error = exc
                            if attempts == 0:
                                budget.release(reservation)
                            else:
                                budget.settle(reservation, input_tokens=usage.get("input_tokens"),
                                    output_tokens=usage.get("output_tokens"), images=image_count,
                                    request_id=request_id, error=error_text, attempts=attempts)
                            if detection is not None:
                                done[key] = detection
                                completed_keys.add(key)
                            save_checkpoint(durable=False)
                        if progress_callback:
                            progress_callback({"phase": "analyzing", "completed": len(completed_keys),
                                "total": len(windows), "results": len(done), "spend_usd": budget.incurred_spend,
                                "remaining_usd": budget.remaining, "active_requests": len(in_flight)})
                except BaseException as exc:
                    gate.abort(exc)
                    raise
                finally:
                    # Undispatched work cost nothing. Retry it on the next batch.
                    next_number -= len(batch)
                    for _, reservation, _ in batch:
                        budget.release(reservation)
                    save_checkpoint()
                    log.info("Analysis batch saved: completed=%s total=%s spend=%.4f", len(completed_keys), len(windows), budget.spend)
        if base_error is not None:
            raise base_error
        print()
        # Leave at least 10% of the total budget for wider game-context checks.
        uncertain = [(key, det) for key, det in list(done.items())
                     if not key.endswith("_refine") and should_refine(det) and f"{key}_refine" not in done]
        for key, det in uncertain:
            if budget.remaining <= 0:
                break
            center = det.estimated_event_timestamp or 0
            refine_times = sorted({max(0, center-0.25), max(0, center-0.125), center,
                                   min(duration, center+0.125), min(duration, center+0.25)})
            reserve = None
            checkpointing = False
            try:
                frames = [get_frame(t) for t in refine_times]
                reserve = budget.reserve(len(frames), refinement=True, attempts=adapter.retries + 1,
                                         ceiling_fraction=0.9)
                checkpointing = True
                save_checkpoint()
                checkpointing = False
                refined, usage = adapter.classify(frames)
                budget.settle(reserve, input_tokens=usage.get("input_tokens") if isinstance(usage, dict) else None,
                              output_tokens=usage.get("output_tokens") if isinstance(usage, dict) else None,
                              images=len(frames), request_id=adapter.last_request_id,
                              attempts=max(1, adapter.last_attempts))
                done[f"{key}_refine"] = refined
            except BudgetExceeded:
                break
            except Exception as exc:
                if checkpointing:
                    budget.release(reserve)
                    raise
                error = f"{key} refinement: {type(exc).__name__}: {exc}"
                errors.append(error)
                if reserve is not None:
                    budget.settle(reserve, input_tokens=None, output_tokens=None, images=len(frames),
                                  request_id=adapter.last_request_id, error=error,
                                  attempts=max(1, adapter.last_attempts))
                if isinstance(exc, RequestFailed):
                    save_checkpoint()
                    raise
            save_checkpoint()

        candidates = merge_detections(effective_detections(done))
        for number, candidate in enumerate(candidates, 1):
            key = candidate_key(candidate)
            if key not in contexts:
                reserve = None
                try:
                    times = context_times(candidate["timestamp"], duration, info.get("frame_rate"))
                    # Reserve before decoding so an exhausted budget does no extra sampling.
                    reserve = budget.reserve(len(times), refinement=True, attempts=adapter.retries + 1)
                    frames = [get_frame(t) for t in times]
                except BudgetExceeded:
                    contexts[key] = ContextResult()
                except Exception as exc:
                    error = f"{key} context sampling: {type(exc).__name__}: {exc}"
                    errors.append(error)
                    if reserve is not None:
                        # No model request was sent, so release the complete reservation.
                        budget.release(reserve)
                    contexts[key] = ContextResult()
                else:
                    save_checkpoint()
                    try:
                        result, usage = adapter.classify_context(frames, candidate["timestamp"])
                        budget.settle(reserve, input_tokens=usage.get("input_tokens"),
                                      output_tokens=usage.get("output_tokens"), images=len(frames),
                                      request_id=adapter.last_request_id,
                                      attempts=max(1, adapter.last_attempts))
                        contexts[key] = result
                    except Exception as exc:
                        error = f"{key} context: {type(exc).__name__}: {exc}"
                        errors.append(error)
                        budget.settle(reserve, input_tokens=None, output_tokens=None, images=len(frames),
                                      request_id=adapter.last_request_id, error=error,
                                      attempts=max(1, adapter.last_attempts))
                        if isinstance(exc, RequestFailed):
                            save_checkpoint()
                            raise
                        contexts[key] = ContextResult()
                save_checkpoint()
            if progress_callback:
                # Worker settlement uses completed/total as analyzed footage, not
                # progress through post-analysis candidate checks.
                progress_callback({"phase": "checking_game_context", "completed": len(completed_keys),
                    "total": len(windows), "context_completed": number, "context_total": len(candidates),
                    "spend_usd": budget.incurred_spend, "remaining_usd": budget.remaining})
        analysis_finished = True
    finally:
        final_detections = effective_detections(done)
        events = write_results(output, info, budget, final_detections, errors, start_time, duration,
                      args.pre_padding, args.post_padding, args.video,
                      create_media=not args.csv_only,
                      complete=len([k for k in done if "_refine" not in k]) >= len(windows),
                      csv_only=args.csv_only,
                      create_reel_output=getattr(args, "create_reel_output", True),
                      context_results=contexts)
        if progress_callback:
            progress_callback({"phase": "complete" if analysis_finished else "analyzing", "completed": len(completed_keys), "total": len(windows),
                "results": len(events), "spend_usd": budget.incurred_spend, "remaining_usd": budget.remaining})
    print(f"Saved {'CSV' if args.csv_only else 'results'} to {output}. Confirmed: {sum(e.classification == 'made_basket' for e in events)}; "
          f"uncertain: {sum(e.classification == 'uncertain' for e in events)}; spend ~${budget.spend:.4f}.")
    return 0


def effective_detections(done: dict[str, Detection]) -> list[tuple[str, Detection]]:
    replaced = {source for detection in done.values()
                if detection.classification != "uncertain" for source in detection.refinement_sources}
    return sorted(
        [(key, det) for key, det in done.items()
         if key not in replaced and not (det.classification == "uncertain" and f"{key}_refine" in done)],
        key=lambda item: (item[1].estimated_event_timestamp if item[1].estimated_event_timestamp is not None
                          else float("inf"), item[0]))


def main() -> int:
    args = parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "evaluate-accuracy":
            print(json.dumps(evaluate_accuracy(json.loads(args.run_state.read_text()),
                json.loads(args.events_json.read_text()), str(args.labels_csv), args.tolerance_seconds), indent=2))
            return 0
        if args.command == "evaluate":
            data = json.loads(args.events_json.read_text())
            predictions = [float(event["timestamp_seconds"]) for event in data
                           if event.get("classification") == "made_basket"]
            result = evaluate_events(predictions, str(args.labels_csv), args.tolerance_seconds)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "evaluate-context":
            state = json.loads(args.run_state.read_text())
            detections = {key: Detection.model_validate(value) for key, value in state.get("detections", {}).items()}
            contexts = {key: ContextResult.model_validate(value) for key, value in state.get("contexts", {}).items()}
            result = evaluate_context(merge_detections(effective_detections(detections)), contexts,
                                      str(args.labels_csv), args.tolerance_seconds)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "highlights":
            clips = generate_from_csv(args.video, args.events_csv, args.output_dir)
            print(f"Generated {len(clips)} clips from CSV in {args.output_dir}")
            if clips:
                print(f"Highlights reel: {args.output_dir / 'highlights_reel.mp4'}")
            return 0
        if args.command == "ui":
            import uvicorn
            from .web import create_app
            uvicorn.run(create_app(args.projects_dir), host=args.host, port=args.port)
            return 0
        return analyze(args)
    except (OSError, ValueError, RuntimeError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
