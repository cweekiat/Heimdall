import assert from "node:assert/strict";
import { test } from "node:test";
import { conflictRadius, conflictText } from "../src/contextmodel.ts";

test("conflict radius grows with sources and is capped", () => {
  assert.equal(conflictRadius(1), 4);
  assert.ok(conflictRadius(8) > conflictRadius(2));
  assert.equal(conflictRadius(10_000), 14);
});

test("conflict hover text names the source count and the caveat", () => {
  const t = conflictText({ label: "Aerial weapons used", place: "Kharkiv, Ukraine", sources: 3, domain: "bbc.co.uk", ts: "2026-10-03T08:00:00Z" }, () => "03 Oct 16:00");
  assert.ok(t.startsWith("Aerial weapons used · Kharkiv, Ukraine · 3 sources (e.g. bbc.co.uk) · 03 Oct 16:00"));
  assert.ok(t.includes("may be wrong or old"));
  assert.ok(conflictText({ sources: 1 }).includes("1 source "));
});
