import assert from "node:assert/strict";
import { test } from "node:test";
import { CHECKS, ended, nextStep, stageOf } from "../src/workflow.ts";

test("every status belongs to exactly one stage", () => {
  assert.equal(stageOf("new"), "action");
  for (const s of ["acknowledged", "under_review", "shelved"] as const) assert.equal(stageOf(s), "progress");
  assert.equal(stageOf("confirmed"), "confirmed");
  for (const s of ["resolved", "dismissed", "expired"] as const) assert.equal(stageOf(s), "closed");
});

test("confirmed alerts ask to be closed once the activity has stopped", () => {
  const now = Date.parse("2026-10-01T12:00:00Z");
  const live = { status: "confirmed" as const, last_seen: "2026-10-01T11:40:00Z" };
  const old = { status: "confirmed" as const, last_seen: "2026-10-01T09:00:00Z" };
  assert.equal(ended(live, now), false);
  assert.equal(ended(old, now), true);
  assert.match(nextStep(live, now), /still active/);
  assert.match(nextStep(old, now), /Close the alert/);
  assert.match(nextStep({ status: "new", last_seen: live.last_seen }, now), /^Acknowledge/);
});

test("the main detection types have checks", () => {
  for (const k of ["gnss_jamming", "gnss_spoofing", "ais_spoofing", "ais_gap", "mil_buildup"]) assert.ok((CHECKS[k] ?? []).length >= 2, k);
});
