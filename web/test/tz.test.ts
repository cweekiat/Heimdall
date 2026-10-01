import assert from "node:assert/strict";
import { test } from "node:test";
const t = await import("../src/tz.ts");

test("default is Singapore time", () => {
  assert.equal(t.getTz(), "Asia/Singapore");
  assert.equal(t.fmtDT("2026-10-01T06:30:00Z"), "10-01 14:30 SGT");
  assert.equal(t.fmtClock("2026-10-01T06:30:05Z"), "2026-10-01 14:30:05 SGT");
});

test("other zones and offsets", () => {
  assert.equal(t.fmtDT("2026-10-01T06:30:00Z", "UTC"), "10-01 06:30 UTC");
  assert.equal(t.tzOffset("Asia/Kolkata", Date.parse("2026-10-01T00:00:00Z")), "UTC+5:30");
  assert.equal(t.tzLabel("America/New_York").startsWith("UTC-"), true);
  assert.equal(t.validTz("Not/AZone"), false);
});
