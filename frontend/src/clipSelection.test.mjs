import { test } from "node:test";
import assert from "node:assert/strict";
import { defaultFreeThrowFilter, includedInReel } from "./clipSelection.ts";

test("free-throw filter defaults on and respects a saved opt-out", () => {
  assert.equal(defaultFreeThrowFilter({}), true);
  assert.equal(defaultFreeThrowFilter({ reel_job: { exclude_free_throws: false } }), false);
  assert.equal(defaultFreeThrowFilter({ latest_reel: { exclude_free_throws: false } }), false);
  assert.equal(defaultFreeThrowFilter({ reel_job: { exclude_free_throws: true }, latest_reel: { exclude_free_throws: false } }), true);
});

test("free-throw filter changes the export without changing saved selections", () => {
  const clip = { included: true, shot_type: "free_throw" };
  assert.equal(includedInReel(clip, false), true);
  assert.equal(includedInReel(clip, true), false);
  assert.equal(clip.included, true);
  assert.equal(includedInReel(clip, false), true);
});

test("unknown shot types remain and deselected clips never return through filtering", () => {
  for (const shot_type of ["unknown", "two_pointer", "three_pointer", "", undefined]) {
    assert.equal(includedInReel({ included: true, shot_type }, true), true);
    assert.equal(includedInReel({ included: false, shot_type }, false), false);
    assert.equal(includedInReel({ included: false, shot_type }, true), false);
  }
  assert.equal(includedInReel({ included: false, shot_type: "free_throw" }, false), false);
});
