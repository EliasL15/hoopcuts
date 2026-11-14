import { test } from "node:test";
import assert from "node:assert/strict";
import { analysisStage } from "./analysisProgress.ts";

test("later passes use their own counts after the footage scan reaches 100%", () => {
  const scanned = { completed: 25512, total: 25512 };
  const refining = analysisStage("running", { ...scanned, phase: "refining_candidates", stage_completed: 12, stage_total: 432 });
  assert.equal(refining.completed, 12);
  assert.equal(refining.total, 432);
  assert.equal(refining.step, 1);
  const clips = analysisStage("running", { ...scanned, phase: "creating_clips", clips_completed: 7, clips_total: 62 });
  assert.equal(clips.completed, 7);
  assert.equal(clips.total, 62);
  assert.equal(clips.unit, "previews prepared");
});

test("older workers show known context counts and honest unknown rendering progress", () => {
  const context = analysisStage("running", { phase: "checking_game_context", completed: 100, total: 100, context_completed: 5, context_total: 12 });
  assert.equal(context.completed, 5);
  assert.equal(context.total, 12);
  assert.equal(analysisStage("running", { phase: "creating_clips", completed: 100, total: 100 }).hasCount, false);
  assert.equal(analysisStage("running", { phase: "analyzing", completed: 100, total: 100 }).phase, "finishing_analysis");
});

test("preparing, restoring and finalizing never reuse stale scan percentages", () => {
  for (const phase of ["downloading_video", "preparing_video", "restoring_progress", "saving_results", "complete"]) {
    assert.equal(analysisStage("running", { phase, completed: 100, total: 100 }).hasCount, false);
  }
  assert.equal(analysisStage("queued", { phase: "creating_clips" }).phase, "queued");
  assert.equal(analysisStage("running", { phase: "refining_candidates", stage_completed: 0, stage_total: 0 }).hasCount, false);
});

test("upload progress uses uploaded previews and clamps invalid overshoot", () => {
  const stage = analysisStage("running", { phase: "uploading_clips", uploads_completed: 20, uploads_total: 12 });
  assert.equal(stage.completed, 12);
  assert.equal(stage.unit, "previews uploaded");
  assert.equal(analysisStage("running", { phase: "creating_clips", clips_completed: NaN, clips_total: 10 }).hasCount, false);
});
