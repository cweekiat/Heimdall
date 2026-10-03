import assert from "node:assert/strict";
import { test } from "node:test";
import { altColor, refLines, trackSegments } from "../src/investigatemodel.ts";

test("altitude ramp: ground grey, interpolated, clamped at the top", () => {
  assert.deepEqual(altColor(-1), [140, 150, 160, 255]);
  assert.deepEqual(altColor(0), [255, 196, 60, 255]);
  const mid = altColor(5000);
  assert.ok(mid[0] < 255 && mid[0] > 130);
  assert.deepEqual(altColor(60000, 0.5), [230, 110, 220, 128]);
});

test("track segments: earlier flights faded, no line across a data gap", () => {
  const t0 = 1_760_000_000;
  const segs = trackSegments({
    track: [
      [0, 50, -1, t0],
      [0.1, 50.1, 5000, t0 + 300],
      [5, 55, 30000, t0 + 3 * 3600], // gap > 90 min: not joined
      [5.1, 55.1, 30000, t0 + 3 * 3600 + 300],
    ],
    flight_from: 2,
  });
  assert.equal(segs.length, 2);
  assert.equal(segs[0]!.color[3], 89); // earlier flight, faded
  assert.equal(segs[1]!.color[3], 242);
});

test("reference lines skip empty fields and say uncrewed", () => {
  const rows = refLines({ type: "Q9", name: "MQ-9", mission: "Armed surveillance drone", range_km: null, endurance_h: 24, crew: 0, refuelling: null, capabilities: "Cameras", source: null });
  assert.deepEqual(rows.map((r) => r[0]), ["Built for", "Endurance", "Crew", "Can do"]);
  assert.equal(rows[2]![1], "uncrewed");
  assert.deepEqual(refLines(null), []);
});
