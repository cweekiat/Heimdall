import assert from "node:assert/strict";
import { test } from "node:test";
import { needText, precisionText } from "../src/tuningmodel.ts";

test("061 tuning text", () => {
  assert.equal(precisionText({ precision: null, precision_ci95: null, reviewed: 0 }), "no reviews yet");
  assert.equal(precisionText({ precision: 0.333, precision_ci95: [0.12, 0.65], reviewed: 9 }), "33 % real of 9 reviewed (likely 12–65 %)");
  const rules = { min_false: 5, min_real: 3, min_gain: 0.3 };
  assert.equal(needText({ real: 1, false: 4 }, rules), "1 more false alarm and 2 more confirmed needed for threshold proposals");
  assert.equal(needText({ real: 3, false: 5 }, rules), "");
});
