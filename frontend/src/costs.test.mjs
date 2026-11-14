import { test } from "node:test";
import assert from "node:assert/strict";
import { cloudComputeCost } from "./costs.ts";

test("active costs update from progress and include saved recovery costs", () => {
  assert.equal(cloudComputeCost({ status: "running", progress: { estimated_compute_usd: .2 }, performance: { estimated_compute_usd: .1 } }), .2);
  assert.equal(cloudComputeCost({ status: "queued", performance: { estimated_compute_usd: .1 } }), .1);
});

test("finished and failed runs use final saved costs rather than stale progress", () => {
  for (const status of ["complete", "failed", "cancelled"]) {
    assert.equal(cloudComputeCost({ status, progress: { estimated_compute_usd: .2 }, performance: { estimated_compute_usd: .3 } }), .3);
  }
});

test("zero is a real cost; missing or invalid values stay unavailable", () => {
  assert.equal(cloudComputeCost({ status: "complete", performance: { estimated_compute_usd: 0 } }), 0);
  for (const value of [undefined, null, NaN, Infinity, -1, ".1"]) {
    assert.equal(cloudComputeCost({ status: "complete", performance: { estimated_compute_usd: value } }), null);
  }
  assert.equal(cloudComputeCost({ status: "complete" }), null);
});
