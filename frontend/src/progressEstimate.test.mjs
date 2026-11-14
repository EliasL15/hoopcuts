import { test } from "node:test";
import assert from "node:assert/strict";
import { observeProgress, estimateRemaining, formatEstimate } from "./progressEstimate.ts";

test("estimates from observed advancement rather than assuming the run started at page load", () => {
  let state = observeProgress(null, "analyzing", 700, 2000, 0);
  assert.equal(estimateRemaining(state, 0), null);
  state = observeProgress(state, "analyzing", 800, 2000, 20);
  assert.equal(estimateRemaining(state, 20), 240);
  assert.equal(formatEstimate(240), "About 4 minutes");
});

test("waits for enough progress and withdraws stale estimates", () => {
  let state = observeProgress(null, "analyzing", 0, 2000, 0);
  state = observeProgress(state, "analyzing", 10, 2000, 20);
  assert.equal(estimateRemaining(state, 20), null);
  state = observeProgress(state, "analyzing", 100, 2000, 25);
  assert.notEqual(estimateRemaining(state, 25), null);
  assert.equal(estimateRemaining(state, 86), null);
  assert.equal(observeProgress(state, "analyzing", 100, 2000, 86), state);
});

test("resets observations on restart, total change and phase change", () => {
  let state = observeProgress(null, "analyzing", 700, 2000, 0);
  state = observeProgress(state, "analyzing", 800, 2000, 20);
  for (const [phase, completed, total] of [["analyzing", 5, 2000], ["analyzing", 800, 3000], ["checking_game_context", 1, 10]]) {
    assert.equal(estimateRemaining(observeProgress(state, phase, completed, total, 25), 25), null);
  }
});

test("does not show zero remaining while final work may still be running", () => {
  let state = observeProgress(null, "analyzing", 1900, 2000, 0);
  state = observeProgress(state, "analyzing", 2000, 2000, 20);
  assert.equal(estimateRemaining(state, 20), null);
});

test("uses the recent two-minute rate instead of old processing speed", () => {
  let state = observeProgress(null, "analyzing", 0, 2000, 0);
  state = observeProgress(state, "analyzing", 100, 2000, 20);
  state = observeProgress(state, "analyzing", 300, 2000, 130);
  state = observeProgress(state, "analyzing", 400, 2000, 150);
  assert.equal(state.samples[0].time, 130);
  assert.equal(estimateRemaining(state, 150), 320);
});

test("context checks have a separate rate with a smaller observation threshold", () => {
  let state = observeProgress(null, "checking_game_context", 1, 10, 0);
  state = observeProgress(state, "checking_game_context", 3, 10, 20);
  assert.equal(estimateRemaining(state, 20), 70);
});

test("formats estimates in rounded minutes and hours", () => {
  assert.equal(formatEstimate(10), "Less than a minute");
  assert.equal(formatEstimate(61), "About 2 minutes");
  assert.equal(formatEstimate(3600), "About 1 hour");
  assert.equal(formatEstimate(4200), "About 1 hour 10 minutes");
  assert.equal(formatEstimate(7200), "About 2 hours");
});

test("refinement and preview estimates use moment counts instead of completed scan windows", () => {
  for (const phase of ["refining_candidates", "creating_clips", "uploading_clips"]) {
    let state = observeProgress(null, phase, 1, 10, 0);
    state = observeProgress(state, phase, 3, 10, 20);
    assert.equal(estimateRemaining(state, 20), 70);
  }
});
