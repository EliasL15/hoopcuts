"""Refinement defaults and experiments stay bounded and recoverable."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from basketball_highlights import cli
from basketball_highlights.evaluate import evaluate_accuracy
from basketball_highlights.events import merge_detections
from basketball_highlights.game_context import candidate_key
from basketball_highlights.models import ContextResult, Detection
from basketball_highlights.refinement import candidate_jobs, settings
from hosted.app.config import Settings, analysis_settings
from test_pipeline_performance import fast_pipeline, legacy_pipeline
from test_billing import env as hosted_env


def detection(center, probability=.2, outcome="uncertain", evidence=None):
    return Detection(classification=outcome, estimated_event_timestamp=center,
                     evidence_timestamps=evidence if evidence is not None else [center-.25, center, center+.25],
                     api_response={"answers": [{"name": "outcome", "probabilities": [
                         {"value": "made_basket", "probability": probability}]}]})


def config(**kwargs):
    return settings(SimpleNamespace(candidate_refinement=True, **kwargs))


def test_clusters_are_bounded_and_preserve_close_distinct_attempts():
    done = {f"w{i}": detection(5+i*.25) for i in range(5)}
    jobs = candidate_jobs(done, 20, config(refinement_frames=9))
    assert [len(job["members"]) for job in jobs] == [3, 2]
    assert len(jobs[0]["samples"]) == 9
    assert jobs[0]["samples"][-1] - jobs[0]["samples"][0] == 1
    separate = candidate_jobs({"a": detection(5, evidence=[5]), "b": detection(5.25, evidence=[5.25])}, 20, config())
    assert len(separate) == 2
    assert candidate_jobs(dict(reversed(list(done.items()))), 20, config(refinement_frames=9)) == jobs


def test_rescue_ranking_cap_and_confirmed_protection():
    done = {"a": detection(2), "b": detection(4, .02), "c": detection(6, .03, "missed_shot"),
            "d": detection(8, .99, "made_basket"), "e": detection(10, .001)}
    jobs = candidate_jobs(done, 20, config(rescue_threshold=.02, rescue_missed=True, rescue_max_requests=1))
    assert [job["members"] for job in jobs] == [["a"], ["c"]]
    assert jobs[-1]["rescue"]
    jobs = candidate_jobs(done, 20, config(rescue_threshold=.02, rescue_max_requests=1))
    assert jobs[-1]["members"] == ["b"]
    assert len(candidate_jobs(done, 20, config(rescue_threshold=.02, rescue_max_requests=0))) == 1


def test_sampling_stays_before_final_frame():
    jobs = candidate_jobs({"a": detection(0)}, .01, config(refinement_frames=9), 25)
    assert jobs[0]["samples"] == [0]
    jobs = candidate_jobs({"a": detection(2)}, 2, config(refinement_frames=9), 25)
    assert max(jobs[0]["samples"]) == 1.96


def test_refinement_replaces_all_members_without_hiding_unresolved_candidates():
    original = {"a": detection(5), "b": detection(5.25)}
    refined = detection(5.25, .99, "made_basket")
    refined.refinement_sources = ["a", "b"]
    effective = cli.effective_detections({**original, "candidate_x_refine": refined})
    assert [key for key, _ in effective] == ["candidate_x_refine"]
    assert merge_detections(effective)[0]["source_windows"] == ["a", "b"]
    refined.classification = "uncertain"
    refined.api_response = {}
    assert {key for key, _ in cli.effective_detections({**original, "candidate_x_refine": refined})} >= {"a", "b"}
    refined.classification = "missed_shot"
    assert merge_detections(cli.effective_detections({**original, "candidate_x_refine": refined})) == []


@pytest.mark.parametrize("prefetch", [0, 4])
@pytest.mark.parametrize("interrupted", [False, True])
def test_candidate_pipeline_and_resume(fast_pipeline, prefetch, interrupted):
    p = fast_pipeline
    p.args.candidate_refinement = True
    p.args.refinement_frames = 9
    p.args.prefetch_requests = prefetch
    p.args.chronological_evidence = bool(prefetch)
    calls = []
    def classify(frames):
        times = [f.timestamp for f in frames]
        calls.append(times)
        if len(times) > 3:
            result = detection(5.25, .99, "made_basket", times)
        else:
            center = times[len(times)//2]
            result = detection(center, .2 if 5 <= center <= 5.25 else .001, evidence=times)
        return result, {"input_tokens": 100}
    p.adapter.classify = classify
    p.adapter.classify_context = lambda *_: (ContextResult(classification="game_play", confidence=.99, checked=True), {})
    if interrupted:
        def interrupt(progress):
            if progress["phase"] == "checking_game_context":
                raise RuntimeError("interrupted before context")
        with pytest.raises(RuntimeError, match="interrupted before context"):
            cli.analyze(p.args, progress_callback=interrupt)
        p.args.resume = True
    cli.analyze(p.args)
    assert len([times for times in calls if len(times) == 9]) == 1
    events = json.loads((p.output / "events.json").read_text())
    assert len(events) == 1 and events[0]["included"]
    assert events[0]["source_windows"] == ["w000019", "w000020"]
    report = json.loads((p.output / "performance.json").read_text())
    assert report["refinement"]["candidate_requests"] == 1
    p.args.resume = True
    p.adapter.classify = lambda *_: pytest.fail("Completed work must not repeat")
    p.adapter.classify_context = p.adapter.classify
    cli.analyze(p.args)
    assert json.loads((p.output / "events.json").read_text()) == events
    p.args.refinement_frames = 5
    with pytest.raises(ValueError, match="Resume settings"):
        cli.analyze(p.args)


def test_stage_metrics_distinguish_context_loss(tmp_path):
    ds = {"a": detection(5, .9, "made_basket"), "b": detection(15, .9, "made_basket"),
          "c": detection(25)}
    candidates = merge_detections(list(ds.items()))
    contexts = {candidate_key(c): ContextResult(classification="warmup_or_break" if c["timestamp"] == 5 else "uncertain",
                                               confidence=.99, checked=True).model_dump() for c in candidates}
    labels = tmp_path / "labels.csv"
    labels.write_text("timestamp_seconds,game_context\n5,game_play\n15,game_play\n25,game_play\n35,game_play\n")
    events = [{"timestamp_seconds": 15, "classification": "made_basket", "included": False},
              {"timestamp_seconds": 25, "classification": "uncertain", "included": False}]
    report = evaluate_accuracy({"detections": {k:v.model_dump() for k,v in ds.items()}, "contexts": contexts}, events, str(labels))
    assert report["candidates_before_context"]["recall"] == .75
    assert report["confirmed_before_context"]["recall"] == .5
    assert report["confirmed_after_context"]["recall"] == .25
    assert report["automatically_selected"]["recall"] == 0
    assert report["context"]["game_baskets_incorrectly_excluded"] == 1


def test_hosted_settings_default_off_and_pin_experiment():
    baseline = replace(Settings(), analysis_candidate_refinement=False, analysis_refinement_frames=5,
                       analysis_rescue_threshold=.05, analysis_rescue_missed=False)
    assert "candidate_refinement" not in analysis_settings(baseline)
    pinned = analysis_settings(replace(baseline, analysis_pipeline_version=2, analysis_candidate_refinement=True,
                                       analysis_refinement_frames=9, analysis_rescue_threshold=.02))
    assert pinned["refinement_frames"] == 9 and pinned["rescue_threshold"] == .02
    assert pinned["rescue_max_requests"] == 0


def test_cli_and_application_defaults_match_hosted_rollout():
    from basketball_highlights.web import DEFAULT_ANALYSIS_CONFIG
    args = cli.parser().parse_args(["analyze", "game.mp4"])
    expected = {"candidate_refinement": True, "refinement_frames": 9,
                "rescue_threshold": .05, "rescue_missed": False, "rescue_max_requests": 0}
    assert settings(args) == expected
    assert {key: DEFAULT_ANALYSIS_CONFIG[key] for key in expected} == expected
    assert Settings.__dataclass_fields__["analysis_candidate_refinement"].default is True
    assert Settings.__dataclass_fields__["analysis_refinement_frames"].default == 9
    assert Settings.__dataclass_fields__["analysis_rescue_max_requests"].default == 0
    legacy = cli.parser().parse_args(["analyze", "game.mp4", "--pipeline-version", "1"])
    assert settings(legacy)["candidate_refinement"] is False
    assert settings(legacy)["refinement_frames"] == 5
    disabled = cli.parser().parse_args(["analyze", "game.mp4", "--no-candidate-refinement"])
    assert not settings(disabled)["candidate_refinement"]
    assert settings(disabled)["refinement_frames"] == 5


@pytest.mark.parametrize("project_mode", ["new", "legacy", "missing"])
def test_local_projects_keep_saved_algorithm(tmp_path, monkeypatch, project_mode):
    import threading
    from basketball_highlights import web
    metadata = {"video": "source.mp4"}
    if project_mode != "missing":
        metadata["analysis_config"] = dict(web.DEFAULT_ANALYSIS_CONFIG if project_mode == "new" else web.LEGACY_ANALYSIS_CONFIG)
    captured = []
    monkeypatch.setattr(web, "analyze", lambda args, **_: captured.append(args) or 0)
    store = SimpleNamespace(directory=lambda _: tmp_path, metadata=lambda _: metadata,
                            events=lambda _: [], save_events=lambda *_: None, jobs={}, lock=threading.RLock())
    store.save_job = lambda key, value: store.jobs.update({key:value})
    web.run_analysis(store, "game")
    assert store.jobs["game"]["status"] == "complete"
    assert settings(captured[0])["candidate_refinement"] is (project_mode == "new")
    assert settings(captured[0])["refinement_frames"] == (9 if project_mode == "new" else 5)


@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("json_only", [False, True])
def test_cli_resume_inherits_original_refinement_settings(fast_pipeline, candidate, json_only):
    p = fast_pipeline
    p.args.candidate_refinement = candidate
    p.args.refinement_frames = 9 if candidate else 5
    cli.analyze(p.args)
    original = json.loads((p.output / "run_state.json").read_text())["signature"]
    if json_only:
        (p.output / "analysis.sqlite3").unlink()
    resumed = cli.parser().parse_args(["analyze", str(p.args.video), "--output-dir", str(p.output),
                                      "--max-concurrency", "1", "--resume"])
    p.adapter.classify = lambda *_: pytest.fail("Completed work must not repeat")
    p.adapter.classify_context = p.adapter.classify
    cli.analyze(resumed)
    assert json.loads((p.output / "run_state.json").read_text())["signature"] == original
    resumed.candidate_refinement = not candidate
    resumed.refinement_frames = 5 if candidate else 9
    with pytest.raises(ValueError, match="Resume settings"):
        cli.analyze(resumed)


def test_hosted_trial_settings_survive_default_rollback(hosted_env, monkeypatch):
    from datetime import timedelta
    from hosted import worker
    from test_media import NOW, RUN
    _, ref = hosted_env
    ref.update({"status": "queued"})
    monkeypatch.setattr(worker, "settings", replace(worker.settings,
        analysis_pipeline_version=2, analysis_candidate_refinement=True,
        analysis_refinement_frames=9, analysis_rescue_threshold=.05,
        analysis_rescue_missed=False, analysis_rescue_max_requests=0))
    first = worker.claim_run("alice", RUN, "first")
    saved = first["analysis_settings"]
    assert saved["candidate_refinement"] and saved["refinement_frames"] == 9
    assert saved["rescue_threshold"] == .05 and not saved["rescue_missed"]
    assert saved["rescue_max_requests"] == 0
    ref.update({"lease_expires_at": NOW - timedelta(minutes=1)})
    monkeypatch.setattr(worker, "settings", replace(worker.settings,
        analysis_candidate_refinement=False, analysis_refinement_frames=5))
    resumed = worker.claim_run("alice", RUN, "resumed")
    assert resumed["analysis_settings"] == saved


@pytest.mark.parametrize("kwargs", [{"rescue_threshold": float("nan")}, {"rescue_max_requests": -1},
                                    {"refinement_frames": 7}])
def test_invalid_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        config(**kwargs)


def test_benchmark_acceptance_requires_recall_precision_and_relative_time(tmp_path, monkeypatch):
    from verification import benchmark_pipeline as benchmark
    for i in range(3):
        (tmp_path / f"video-{i}.mp4").touch()
    labels = tmp_path / "labels.csv"
    labels.write_text("timestamp_seconds,game_context\n5,game_play\n15,game_play\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"label_source": "human", "games": [
        {"id": str(i), "split": "tuning" if i == 0 else "validation", "video": f"video-{i}.mp4",
         "labels_csv": "labels.csv", "camera_style": "fixed"} for i in range(3)]}))
    ticks = iter([0, 10, 20, 30.9, 40, 50, 60, 71.1, 80, 90, 100, 110])
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: next(ticks))
    def analyze(options):
        assert options.group_windows == 8 and options.prefetch_requests == 4
        assert options.decoder_thread_type == "AUTO"
        folder = options.output_dir
        folder.mkdir()
        timestamps = [5, 15] if options.candidate_refinement else [5]
        if "game-2-" in folder.name and options.candidate_refinement:
            timestamps.append(25)  # Extra false highlight must fail the gate.
        ds = {f"w{i}": detection(t, .9, "made_basket") for i, t in enumerate(timestamps)}
        cs = merge_detections(list(ds.items()))
        state = {"detections": {k:v.model_dump() for k,v in ds.items()}, "contexts": {
            candidate_key(c): ContextResult(classification="game_play", confidence=.99, checked=True).model_dump() for c in cs}}
        (folder / "run_state.json").write_text(json.dumps(state))
        (folder / "events.json").write_text(json.dumps([
            {"timestamp_seconds": t, "classification": "made_basket", "included": True} for t in timestamps]))
        (folder / "processing_summary.json").write_text(json.dumps({"budget": {"estimated_spend_usd": .01},
            "video": {"duration_seconds": 30}, "complete": True}))
    monkeypatch.setattr(benchmark.cli, "analyze", analyze)
    args = SimpleNamespace(manifest=manifest, output=tmp_path / "matrix", execute=True,
        profiles=["hosted"], accuracy_profiles=["baseline", "combined"], groups=[8],
        concurrencies=[4], cpu=2, memory_gib=8, budget_usd=5, decoder_threads=2)
    benchmark.games(args)
    rows = json.loads((args.output / "matrix.json").read_text())
    assert rows[1]["accepted"]
    assert not rows[3]["accepted"] and not rows[3]["time_passed"]
    assert not rows[5]["accepted"] and not rows[5]["quality_passed"]
    summaries = json.loads((args.output / "accuracy-summary.json").read_text())
    combined = {r["split"]: r for r in summaries if r["accuracy_profile"] == "combined"}
    assert combined["tuning"]["accepted"]
    assert not combined["validation"]["accepted"]
