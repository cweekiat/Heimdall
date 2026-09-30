import assert from "node:assert/strict";
import { test } from "node:test";
const { TourController, tourSeen, markTourSeen, TOUR_FLAG } = await import("../src/tour/controller.ts");
const { TOUR_STEPS } = await import("../src/tour/steps.ts");

function make(missing: string[] = []) {
  const log: (string | null)[] = [];
  let ended = 0;
  const c = new TourController({
    prepare: () => undefined,
    exists: (sel: string) => !missing.includes(sel),
    onChange: (s) => log.push(s ? s.id : null),
    onEnd: () => void ended++,
  });
  return { c, log, ended: () => ended };
}

test("step list follows the workflow and covers the required anchors", () => {
  const ids = TOUR_STEPS.map((s) => s.id);
  for (const k of ["toolbar", "map", "layers", "queue", "details", "evidence", "trust", "actions", "log", "sources"]) assert.ok(ids.includes(k), k);
  const body = TOUR_STEPS.map((s) => s.body).join(" ");
  assert.match(body, /52 km/);
  assert.match(body, /not a single aircraft/);
  assert.match(body, /amber is military/);
  for (const s of TOUR_STEPS) assert.ok(s.title && s.body && s.anchor);
});

test("next, back and skip", () => {
  const { c, log, ended } = make();
  c.start();
  c.next();
  c.next();
  c.back();
  assert.deepEqual(log, [TOUR_STEPS[0]!.id, TOUR_STEPS[1]!.id, TOUR_STEPS[2]!.id, TOUR_STEPS[1]!.id]);
  c.end();
  assert.equal(c.active, false);
  assert.equal(ended(), 1);
  assert.equal(log.at(-1), null);
});

test("finishing on the last step ends the tour", () => {
  const { c, ended } = make();
  c.start();
  for (let i = 0; i < TOUR_STEPS.length; i++) c.next();
  assert.equal(c.active, false);
  assert.equal(ended(), 1);
});

test("relaunch resets to step 1", () => {
  const { c, log } = make();
  c.start();
  c.next();
  c.start();
  assert.equal(log.at(-1), TOUR_STEPS[0]!.id);
  assert.equal(c.index, 0);
});

test("missing anchor is skipped both ways, without a crash", () => {
  const { c, log } = make([TOUR_STEPS[1]!.anchor, TOUR_STEPS[0]!.anchor]);
  c.start();
  assert.equal(log[0], TOUR_STEPS[2]!.id);
  c.next();
  assert.equal(log[1], TOUR_STEPS[3]!.id);
  c.back();
  assert.equal(log[2], TOUR_STEPS[2]!.id);
  c.back();
  assert.equal(log.length, 3);
});

test("all anchors missing ends immediately", () => {
  const { c, ended } = make(TOUR_STEPS.map((s) => s.anchor));
  c.start();
  assert.equal(c.active, false);
  assert.equal(ended(), 1);
});

test("localStorage flag prevents second auto-start; throwing storage is tolerated", () => {
  const m = new Map<string, string>();
  const store = { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) };
  assert.equal(tourSeen(store), false);
  markTourSeen(store);
  assert.equal(m.get(TOUR_FLAG), "1");
  assert.equal(tourSeen(store), true);
  const bad = {
    getItem: (): string | null => {
      throw new Error("blocked");
    },
    setItem: (): void => {
      throw new Error("blocked");
    },
  };
  assert.equal(tourSeen(bad), false);
  assert.doesNotThrow(() => markTourSeen(bad));
  assert.equal(tourSeen(null), false);
});
