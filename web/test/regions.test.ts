import assert from "node:assert/strict";
import { test } from "node:test";
import { findRegions, REGIONS } from "../src/regions.ts";

test("regions are found by name and keyword, empty query finds none", () => {
  assert.deepEqual(findRegions("hormuz").map((r) => r.name), ["Gulf and Hormuz"]);
  assert.ok(findRegions("kalin").some((r) => r.name === "Baltic"));
  assert.ok(findRegions("Taiwan").some((r) => r.name === "Taiwan Strait"));
  assert.deepEqual(findRegions("  "), []);
  assert.ok(REGIONS.every((r) => Math.abs(r.lat) <= 85 && Math.abs(r.lon) <= 180 && r.zoom > 0));
});
