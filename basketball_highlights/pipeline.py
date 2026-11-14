"""Version 2 analysis: bounded decoding, incremental persistence, pooled stages."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from bisect import bisect_left
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .budget import Budget, BudgetExceeded
from .decisions import DecisionsAdapter
from .events import make_windows, merge_detections, should_refine
from .game_context import ANALYSIS_VERSION, candidate_key, context_times
from .journal import Journal
from .models import ContextResult, Detection
from .performance import Performance
from .retry import RequestFailed, RequestGate
from .refinement import candidate_jobs, settings as refinement_settings
from .scheduling import prepare_jobs
from .video import FrameReader, ffprobe, sample_times

log = logging.getLogger(__name__)


def window_samples(times, left, right):
    samples = times[bisect_left(times, left):bisect_left(times, right)] or [left]
    return samples if len(samples) <= 3 else [samples[round(i * (len(samples)-1) / 2)] for i in range(3)]


def analyze(args, progress_callback=None, checkpoint_callback=None, source_identity=None, request_allowance=None):
    from .cli import effective_detections, write_results

    if not args.video.is_file():
        raise FileNotFoundError(f"Video file not found: {args.video}")
    concurrency = getattr(args, "max_concurrency", 4)
    group_size = getattr(args, "group_windows", 1)
    refinement_config = refinement_settings(args)
    refinement_report = {}
    prefetch = getattr(args, "prefetch_requests", 0)
    rolling = getattr(args, "rolling_scheduler", False)
    chronological = getattr(args, "chronological_evidence", False)
    decoder_type = getattr(args, "decoder_thread_type", "SLICE")
    decoder_threads = getattr(args, "decoder_threads", 2)
    if not 0 <= prefetch <= 16 or (rolling and not prefetch):
        raise ValueError("Prefetch must be 0-16; rolling scheduling requires prefetch")
    if decoder_type not in ("SLICE", "AUTO", "FRAME") or decoder_threads not in (1, 2, 4):
        raise ValueError("Invalid decoder threading settings")
    if not 1 <= concurrency <= 16 or group_size not in (1, 4, 8):
        raise ValueError("Concurrency must be 1-16 and window grouping must be 1, 4 or 8")
    perf = Performance()
    info = ffprobe(args.video)
    duration = min(info["duration_seconds"], args.limit_seconds) if args.limit_seconds else info["duration_seconds"]
    info["analyzed_duration_seconds"] = duration
    times = sample_times(duration, args.sampling_fps)
    windows = make_windows(duration, args.window_seconds, args.stride_seconds)
    output = args.output_dir or Path("outputs") / args.video.stem
    state_dir = output / ".state" if args.csv_only else output
    state_dir.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        print(f"Dry run: {len(windows)} windows, {(len(windows)+group_size-1)//group_size} base requests; no API calls")
        return 0
    signature = {"analysis_version": ANALYSIS_VERSION, "pipeline_version": 2, "group_windows": group_size,
                 "video": str(args.video.resolve()), "size": args.video.stat().st_size,
                 "mtime_ns": args.video.stat().st_mtime_ns, "duration": duration,
                 "sampling_fps": args.sampling_fps, "window_seconds": args.window_seconds,
                 "stride_seconds": args.stride_seconds, "max_image_dimension": args.max_image_dimension,
                 "jpeg_quality": args.jpeg_quality, "pre_padding": args.pre_padding,
                 "post_padding": args.post_padding, "budget_usd": args.budget_usd}
    if source_identity is not None:
        signature["source"] = source_identity
        for field in ("video", "size", "mtime_ns"):
            signature.pop(field)
    if refinement_config["candidate_refinement"]:
        signature["refinement"] = {"version": 1, **refinement_config}
    if getattr(args, "timestamp_decoder", False):
        signature["timestamp_decoder"] = "pts-v1"
    # Omit legacy defaults so old checkpoint signatures remain compatible.
    execution = {"prefetch_requests": prefetch, "rolling_scheduler": rolling,
                 "chronological_evidence": chronological,
                 "decoder_thread_type": decoder_type, "decoder_threads": decoder_threads}
    if execution != {"prefetch_requests": 0, "rolling_scheduler": False, "chronological_evidence": False,
                     "decoder_thread_type": "SLICE", "decoder_threads": 2}:
        signature["execution"] = execution
    journal_path = state_dir / "analysis.sqlite3"
    if not args.resume and (journal_path.exists() or (state_dir / "budget.json").exists()):
        raise ValueError("Analysis state already exists; use --resume or a new output directory")
    journal = Journal(journal_path)
    try:
        saved_signature = journal.meta("signature")
        saved = None
        if args.resume and saved_signature is None and (state_dir / "run_state.json").exists():
            saved = json.loads((state_dir / "run_state.json").read_text())
            saved_signature = saved["signature"]
        if args.resume and saved_signature is not None:
            refinement_config = refinement_settings(args, saved_signature.get("refinement", {}))
            signature.pop("refinement", None)
            if refinement_config["candidate_refinement"]:
                signature["refinement"] = {"version": 1, **refinement_config}
        if saved_signature is not None and saved_signature != signature:
            raise ValueError("Resume settings, source video or analysis version differ from the saved run")
        if saved is not None:
            with journal.transaction():
                journal.set_meta("signature", saved_signature)
                journal.set_meta("errors", saved.get("errors", []))
                for table in ("detections", "contexts"):
                    for key, value in saved.get(table, {}).items():
                        journal.put(table, key, value)
        journal.set_meta("signature", signature)
        budget = Budget(state_dir / "budget.json", args.budget_usd, journal=journal)
        done = {key: Detection.model_validate(value) for key, value in journal.records("detections").items()}
        contexts = {key: ContextResult.model_validate(value) for key, value in journal.records("contexts").items()}
        errors = journal.meta("errors", [])
        completed = {key for key in done if not key.endswith("_refine")}
        restored_windows = len(completed)
        readers = {}
        caches = {"base": OrderedDict(), "extra": OrderedDict()}
        adapters = queue.Queue()
        for _ in range(concurrency):
            adapter = DecisionsAdapter()
            adapter.retries, adapter.performance = 3, perf
            adapter.allowance = request_allowance
            adapters.put(adapter)
        worker_state = threading.local()
        gate = RequestGate(concurrency)
        gate.performance = perf
        last_progress = float("-inf")
        current_phase = "analyzing"
        pass_keys = set()

        def progress(phase, force=False, **extra):
            nonlocal last_progress
            now = time.monotonic()
            if progress_callback and (force or now - last_progress >= 5):
                progress_callback({"phase": phase, "completed": len(completed), "total": len(windows),
                    "results": len(done), "spend_usd": budget.incurred_spend,
                    "remaining_usd": budget.remaining,
                    "stage_completed": (len(completed) if phase == "analyzing" else
                        sum(key in (contexts if phase == "checking_game_context" else done) for key in pass_keys)),
                    "stage_total": len(windows) if phase == "analyzing" else len(pass_keys), **extra})
                last_progress = now

        gate.on_wait = lambda: progress(current_phase)

        def initialize():
            worker_state.adapter = adapters.get_nowait()
            worker_state.adapter.gate = gate

        def frame(timestamp, stream, phase="base_analysis"):
            cache = caches[stream]
            if timestamp not in cache:
                with perf.measure("decoding"):
                    if stream not in readers:
                        readers[stream] = FrameReader(args.video, args.max_image_dimension, args.jpeg_quality,
                            sequential=stream == "base", info=info,
                            timestamp_aware=getattr(args, "timestamp_decoder", False),
                            decoder_thread_type=decoder_type, decoder_threads=decoder_threads, performance=perf)
                    readers[stream].phase = phase
                    cache[timestamp] = readers[stream].sample(timestamp)
                while len(cache) > 64:
                    cache.popitem(last=False)
            cache.move_to_end(timestamp)
            return cache[timestamp]

        first_checkpoint = True
        compact_at = len(completed)
        compact_requests = 0

        def checkpoint():
            nonlocal first_checkpoint, compact_at, compact_requests
            compact = (first_checkpoint or len(completed) - compact_at >= 256
                       or budget.data["request_count"] - compact_requests >= 256)
            with perf.measure("persistence"):
                journal.set_meta("errors", errors)
                if checkpoint_callback:
                    state, ledger = journal.checkpoint(signature, errors, len(completed), compact=compact)
                    checkpoint_callback(state, ledger)
                journal.published()
            if compact:
                compact_at = len(completed)
                compact_requests = budget.data["request_count"]
            first_checkpoint = False

        def classify(job, frames):
            adapter = worker_state.adapter
            adapter.last_attempts, adapter.last_request_id = 0, None
            try:
                if job["kind"] == "base":
                    if group_size == 1:
                        key = next(iter(frames))
                        value, usage = adapter.classify(frames[key])
                        result = {key: value}
                    else:
                        result, usage = adapter.classify_group(frames)
                elif job["kind"] == "refinement":
                    result, usage = adapter.classify(frames)
                else:
                    result, usage = adapter.classify_context(frames, job["timestamp"])
                return result, usage, max(1, adapter.last_attempts), adapter.last_request_id, None
            except Exception as exc:
                return None, {}, adapter.last_attempts, adapter.last_request_id, exc

        def legacy_stage(executor, jobs, phase):
            pending = deque(jobs)
            failure = None
            with perf.measure(phase):
                while pending and failure is None:
                    batch = deque()
                    # Reserve and durably publish the entire bounded batch before sending.
                    while pending and len(batch) < 32:
                        job = pending[0]
                        samples = job["samples"]
                        count = len(set(t for values in samples.values() for t in values)) if job["kind"] == "base" else len(samples)
                        try:
                            reserve = budget.reserve(count, attempts=4, refinement=job["kind"] != "base",
                                ceiling_fraction=.9 if job["kind"] == "refinement" else None,
                                text_tokens=300 * len(samples) if job["kind"] == "base" else None)
                        except BudgetExceeded as exc:
                            if batch:
                                break
                            if job["kind"] == "base":
                                failure = exc
                                errors.append(str(exc))
                            elif job["kind"] == "context":
                                with journal.transaction():
                                    for remaining in pending:
                                        result = ContextResult()
                                        contexts[remaining["key"]] = result
                                        journal.put("contexts", remaining["key"], result.model_dump())
                                pending.clear()
                            else:
                                pending.clear()
                            break
                        batch.append((pending.popleft(), reserve, count))
                    checkpoint()
                    active = {}
                    batch_started = time.monotonic()
                    try:
                        while batch or active:
                            while batch and len(active) < gate.concurrency and failure is None and time.monotonic() - batch_started < 30:
                                job, reserve, count = batch.popleft()
                                try:
                                    frames = ({key: [frame(t, "base", phase) for t in values] for key, values in job["samples"].items()}
                                              if job["kind"] == "base" else [frame(t, "extra", phase) for t in job["samples"]])
                                except Exception as exc:
                                    budget.release(reserve)
                                    errors.append(f"{job['key']}: frame decoding failed")
                                    if job["kind"] == "base":
                                        failure = exc
                                    elif job["kind"] == "context":
                                        result = ContextResult()
                                        contexts[job["key"]] = result
                                        journal.put("contexts", job["key"], result.model_dump())
                                    continue
                                active[executor.submit(classify, job, frames)] = job, reserve, count
                            if not active:
                                break
                            finished, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                            for future in finished:
                                job, reserve, count = active.pop(future)
                                result, usage, attempts, request_id, exc = future.result()
                                error = f"{job['key']}: {type(exc).__name__}: {exc}" if exc else None
                                with journal.transaction(), perf.measure("persistence"):
                                    if attempts == 0:
                                        budget.release(reserve)
                                    else:
                                        budget.settle(reserve, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                                            images=count, request_id=request_id, error=error, attempts=attempts)
                                    if exc:
                                        errors.append(error)
                                        if job["kind"] == "base" or isinstance(exc, RequestFailed):
                                            failure = failure or exc
                                        elif job["kind"] == "context":
                                            result = ContextResult()
                                    if result is not None:
                                        if job["kind"] == "base":
                                            for key, detection in result.items():
                                                done[key] = detection
                                                completed.add(key)
                                                journal.put("detections", key, detection.model_dump())
                                        elif job["kind"] == "refinement":
                                            result.refinement_sources = job.get("members", [])
                                            done[job["key"]] = result
                                            journal.put("detections", job["key"], result.model_dump())
                                        else:
                                            contexts[job["key"]] = result
                                            journal.put("contexts", job["key"], result.model_dump())
                            progress(current_phase, active_requests=len(active),
                                     context_completed=len(contexts), context_total=len(contexts)+len(pending)+len(batch)+len(active))
                    except BaseException as exc:
                        gate.abort(exc)
                        # Executor drains already-sent requests; conservative reservations
                        # remain durable if the caller stops before their results settle.
                        raise
                    finally:
                        for job, reserve, _ in reversed(batch):
                            budget.release(reserve)
                            pending.appendleft(job)
                        checkpoint()
                    log.info("Analysis batch saved: phase=%s completed=%s total=%s spend=%.4f", phase, len(completed), len(windows), budget.spend)
                if failure:
                    raise failure

        decoder_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="frame-producer") if prefetch else None

        def optimized_stage(executor, jobs, phase):
            pending = deque(jobs)
            unsent = OrderedDict()
            to_prepare, ready = deque(), deque()
            active = {}
            preparation = None
            failure = None
            last_save = time.monotonic()
            saved_requests = budget.data["request_count"]
            last_tick = last_save

            def save():
                nonlocal last_save, saved_requests
                with perf.measure("checkpoint_blocking"):
                    checkpoint()
                last_save = time.monotonic()
                saved_requests = budget.data["request_count"]

            def settle_result(future):
                nonlocal failure
                job, reserve, count = active.pop(future)
                result, usage, attempts, request_id, exc = future.result()
                error = f"{job['key']}: {type(exc).__name__}: {exc}" if exc else None
                with journal.transaction(), perf.measure("persistence"):
                    if attempts == 0:
                        budget.release(reserve)
                    else:
                        budget.settle(reserve, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                                      images=count, request_id=request_id, error=error, attempts=attempts)
                    if exc:
                        errors.append(error)
                        if job["kind"] == "base" or isinstance(exc, RequestFailed):
                            failure = failure or exc
                        elif job["kind"] == "context":
                            result = ContextResult()
                    if result is not None:
                        if job["kind"] == "base":
                            for key, value in result.items():
                                done[key] = value
                                completed.add(key)
                                journal.put("detections", key, value.model_dump())
                        elif job["kind"] == "refinement":
                            result.refinement_sources = job.get("members", [])
                            done[job["key"]] = result
                            journal.put("detections", job["key"], result.model_dump())
                        else:
                            contexts[job["key"]] = result
                            journal.put("contexts", job["key"], result.model_dump())

            with perf.measure(phase):
                try:
                    while pending or unsent or active or preparation:
                        now = time.monotonic()
                        perf.add(phase + ".occupied_slot_seconds", (now-last_tick)*len(active), count=0)
                        perf.add(phase + ".available_slot_seconds", (now-last_tick)*max(gate.concurrency, len(active)), count=0)
                        last_tick = now
                        perf.observe("prepared_queue_depth", len(ready))
                        perf.observe("outstanding_requests", len(unsent)+len(active))
                        perf.sample_memory()
                        for future in list(active):
                            if future.done():
                                settle_result(future)
                        if failure:
                            break

                        # Only the coordinator reserves and publishes. Capacity includes
                        # sent requests, so recovery exposure never exceeds 32 requests.
                        refill = (len(unsent) <= prefetch if rolling else not unsent and not active)
                        if pending and refill:
                            admitted = []
                            while pending and len(unsent)+len(active) < 32:
                                job = pending[0]
                                samples = job["samples"]
                                count = (len({t for values in samples.values() for t in values})
                                         if job["kind"] == "base" else len(samples))
                                try:
                                    reserve = budget.reserve(count, attempts=4, refinement=job["kind"] != "base",
                                        ceiling_fraction=.9 if job["kind"] == "refinement" else None,
                                        text_tokens=300*len(samples) if job["kind"] == "base" else None)
                                except BudgetExceeded as exc:
                                    # Sent results may release unused retry allowances.
                                    # Wait for admitted work before deciding budget exhaustion.
                                    if not unsent and not active:
                                        if job["kind"] == "base":
                                            failure = exc
                                            errors.append(str(exc))
                                        elif job["kind"] == "context":
                                            with journal.transaction():
                                                for remaining in pending:
                                                    result = ContextResult()
                                                    contexts[remaining["key"]] = result
                                                    journal.put("contexts", remaining["key"], result.model_dump())
                                        pending.clear()
                                    break
                                entry = pending.popleft(), reserve, count
                                unsent[job["key"]] = entry
                                admitted.append(entry)
                            if admitted:
                                save()  # Must succeed before preparation can dispatch any new work.
                                to_prepare.extend(admitted)
                            if failure:
                                break

                        if preparation and preparation.done():
                            ready.extend(preparation.result())
                            perf.observe("prepared_queue_depth", len(ready))
                            preparation = None
                        while ready and len(active) < gate.concurrency:
                            entry, frames, exc = ready.popleft()
                            job, reserve, _ = entry
                            unsent.pop(job["key"])
                            if exc:
                                budget.release(reserve)
                                errors.append(f"{job['key']}: frame decoding failed")
                                if job["kind"] == "base":
                                    failure = exc
                                    break
                                if job["kind"] == "context":
                                    result = ContextResult()
                                    contexts[job["key"]] = result
                                    journal.put("contexts", job["key"], result.model_dump())
                            else:
                                active[executor.submit(classify, job, frames)] = entry
                        if failure:
                            break
                        if preparation is None and to_prepare and len(ready) < prefetch:
                            entries = [to_prepare.popleft() for _ in range(min(prefetch-len(ready), len(to_prepare)))]
                            preparation = decoder_executor.submit(prepare_jobs, entries, frame, phase, chronological)
                        if now-last_save >= 30 or budget.data["request_count"]-saved_requests >= 32:
                            save()
                        progress(current_phase,
                                 active_requests=len(active), prepared_requests=len(ready),
                                 context_completed=len(contexts),
                                 context_total=len(contexts)+len(pending)+len(unsent)+len(active))
                        waiting = [*active, *([preparation] if preparation else [])]
                        if waiting:
                            with perf.measure(phase + ".scheduler_wait"):
                                wait(waiting, timeout=.05, return_when=FIRST_COMPLETED)
                    # A request failure stops admission; already-sent work still settles.
                    for future in list(active):
                        settle_result(future)
                except BaseException as exc:
                    gate.abort(exc)
                    raise
                finally:
                    # Never release sent requests here: interrupted results retain their
                    # durable conservative reservations. Decoder futures never send.
                    if preparation:
                        preparation.result()
                    for _, reserve, _ in unsent.values():
                        budget.release(reserve)
                    save()
                if failure:
                    raise failure

        def stage(executor, jobs, phase, keys=None):
            nonlocal current_phase, pass_keys
            current_phase = {"base_analysis": "analyzing", "refinement": "refining_candidates", "context": "checking_game_context"}[phase]
            pass_keys = set(keys or (job["key"] for job in jobs))
            extra = ({"context_completed": sum(key in contexts for key in pass_keys), "context_total": len(pass_keys)}
                     if phase == "context" else {})
            progress(current_phase, force=True, **extra)
            (optimized_stage if prefetch else legacy_stage)(executor, jobs, phase)
            progress(current_phase, force=True)

        finished = False
        try:
            checkpoint()
            base_jobs, group = [], {}
            for number, (left, right) in enumerate(windows):
                key = f"w{number:06d}"
                if key in done:
                    continue
                group[key] = window_samples(times, left, right)
                if len(group) == group_size:
                    base_jobs.append({"kind": "base", "key": next(iter(group)), "samples": group})
                    group = {}
            if group:
                base_jobs.append({"kind": "base", "key": next(iter(group)), "samples": group})
            with ThreadPoolExecutor(max_workers=concurrency, initializer=initialize, thread_name_prefix="analysis") as executor:
                stage(executor, base_jobs, "base_analysis")
                refinements = []
                refinement_keys = set()
                for key, detection in done.items():
                    if not key.endswith("_refine") and should_refine(detection):
                        refinement_keys.add(f"{key}_refine")
                        if f"{key}_refine" in done:
                            continue
                        center = detection.estimated_event_timestamp or 0
                        samples = sorted({max(0, center-.25), max(0, center-.125), center,
                                          min(duration, center+.125), min(duration, center+.25)})
                        refinements.append({"kind": "refinement", "key": f"{key}_refine", "samples": samples})
                if refinement_config["candidate_refinement"]:
                    planned = candidate_jobs(done, duration, refinement_config, info.get("frame_rate"))
                    refinement_keys = {job["key"] for job in planned}
                    refinements = [job for job in planned if job["key"] not in done]
                    refinement_report = {
                        "eligible_windows": sum(should_refine(d) for key, d in done.items()
                                                if not key.endswith("_refine")),
                        "candidate_requests": len(planned),
                        "rescue_requests": sum(job["rescue"] for job in planned),
                        "settings": refinement_config,
                    }
                stage(executor, refinements, "refinement", refinement_keys)
                candidates = merge_detections(effective_detections(done))
                context_jobs = [{"kind": "context", "key": candidate_key(candidate), "timestamp": candidate["timestamp"],
                    "samples": context_times(candidate["timestamp"], duration, info.get("frame_rate"))}
                    for candidate in candidates if candidate_key(candidate) not in contexts]
                stage(executor, context_jobs, "context", {candidate_key(candidate) for candidate in candidates})
            checkpoint()
            finished = True
        finally:
            def close_readers():
                for reader in readers.values():
                    reader.close()
            if decoder_executor:
                decoder_executor.submit(close_readers).result()
                decoder_executor.shutdown(wait=True)
            else:
                close_readers()
            journal.set_meta("errors", errors)
            with perf.measure("persistence"):
                journal.export(state_dir)
            render_error = None
            try:
                # Keep existing result formats; clip rendering is bounded separately.
                with perf.measure("clips"):
                    write_results(output, info, budget, effective_detections(done), errors, perf.started, duration,
                        args.pre_padding, args.post_padding, args.video, create_media=not args.csv_only,
                        complete=len(completed) == len(windows), csv_only=args.csv_only,
                        create_reel_output=getattr(args, "create_reel_output", True), context_results=contexts,
                        media_workers=2, preview_max_dimension=getattr(args, "preview_max_dimension", None),
                        media_progress=lambda **counts: progress("creating_clips", force="clips_completed" in counts and counts["clips_completed"] == 0,
                            stage_completed=counts.get("clips_completed", 0), stage_total=counts.get("clips_total", 0), **counts),
                        media_ready=getattr(args, "media_ready", None))
            except BaseException as exc:
                render_error = exc
                raise
            finally:
                report = perf.report(len(completed)-restored_windows)
                report.update(pipeline_version=2, group_windows=group_size, max_concurrency=concurrency,
                    timestamp_decoder=getattr(args, "timestamp_decoder", False),
                    execution=execution, resumed=args.resume,
                    restored_windows=restored_windows, processed_windows_this_execution=len(completed)-restored_windows,
                              model_spend_usd=budget.incurred_spend, completed_windows=len(completed))
                if refinement_config["candidate_refinement"]:
                    report["refinement"] = refinement_report
                (state_dir / "performance.json").write_text(json.dumps(report, indent=2))
                summary_path = output / "processing_summary.json"
                if summary_path.exists():
                    summary = json.loads(summary_path.read_text())
                    summary["performance"] = report
                    summary_path.write_text(json.dumps(summary, indent=2))
                if render_error is None:
                    progress("complete" if finished else "analyzing", force=True)
        return 0
    finally:
        journal.close()
