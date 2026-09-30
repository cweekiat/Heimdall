import assert from "node:assert/strict";
import { test } from "node:test";
import { boxInside, decode, densityOpacity, EntityStore, iconOpacity, inBox, padBox, truncationNote, wanted, zoomParam } from "../src/trafficdata.ts";
import type { Box } from "../src/trafficdata.ts";
import { Feed } from "../src/trafficfeed.ts";
import type { FeedReq } from "../src/trafficfeed.ts";

type R = { id: string; lon: number; lat: number; v?: number };
const mk = () => new EntityStore<R>((r) => r.id, 60_000);

test("padBox grows 50 percent, clamps latitude, world stays world, antimeridian wraps", () => {
  assert.deepEqual(padBox([10, 0, 20, 10]), [5, -5, 25, 15]);
  assert.deepEqual(padBox([-10, 80, 10, 90]), [-20, 75, 20, 90]);
  assert.deepEqual(padBox([-180, -60, 180, 60]), [-180, -90, 180, 90]);
  const p = padBox([170, 0, -170, 10]); // 20 degrees wide across the antimeridian
  assert.deepEqual(p, [160, -5, -160, 15]);
  assert.ok(inBox(p, 175, 5) && inBox(p, -165, 5) && !inBox(p, 0, 5));
});

test("boxInside handles plain, world and antimeridian boxes", () => {
  assert.ok(boxInside([12, 1, 18, 9], [5, -5, 25, 15]));
  assert.ok(!boxInside([2, 1, 18, 9], [5, -5, 25, 15]));
  assert.ok(boxInside([-50, -10, 50, 10], [-180, -90, 180, 90]));
  assert.ok(boxInside([172, 0, -172, 5], [160, -5, -160, 15]));
  assert.ok(!boxInside([150, 0, -172, 5], [160, -5, -160, 15]));
});

test("replaceFull merges by id and keeps fresh rows that left the new response", () => {
  const s = mk();
  const prev: Box = [0, 0, 20, 10];
  s.replaceFull([{ id: "a", lon: 5, lat: 5 }, { id: "b", lon: 15, lat: 5 }], 0, prev, null);
  assert.equal(s.size, 2);
  const listA = s.list();
  // view pans east: the new request covers 10..30; "a" (lon 5) is outside it but inside the old box
  s.replaceFull([{ id: "b", lon: 15, lat: 5 }, { id: "c", lon: 25, lat: 5 }], 1000, [10, 0, 30, 10], prev);
  assert.deepEqual(s.list().map((r) => r.id).sort(), ["a", "b", "c"]);
  assert.notEqual(s.list(), listA);
});

test("rows inside the new request but missing from the response are removed", () => {
  const s = mk();
  const box: Box = [0, 0, 20, 10];
  s.replaceFull([{ id: "a", lon: 5, lat: 5 }], 0, box, null);
  s.replaceFull([], 1000, box, box);
  assert.equal(s.size, 0);
});

test("stale rows outside the new request expire; rows outside the old box are dropped", () => {
  const s = mk();
  const prev: Box = [0, 0, 20, 10];
  s.replaceFull([{ id: "a", lon: 5, lat: 5 }, { id: "z", lon: 50, lat: 5 }], 0, prev, null);
  s.replaceFull([], 61_000, [10, 0, 30, 10], prev);
  assert.equal(s.size, 0); // "a" is stale, "z" was never inside the old box
});

test("unchanged rows keep object identity and the list identity/version do not change", () => {
  const s = mk();
  const box: Box = [0, 0, 20, 10];
  s.replaceFull([{ id: "a", lon: 5, lat: 5, v: 1 }], 0, box, null);
  const list = s.list();
  const v = s.version;
  const changed = s.replaceFull([{ id: "a", lon: 5, lat: 5, v: 1 }], 1000, box, box);
  assert.equal(changed, false);
  assert.equal(s.list(), list);
  assert.equal(s.version, v);
  assert.equal(s.replaceFull([{ id: "a", lon: 6, lat: 5, v: 1 }], 2000, box, box), true);
  assert.notEqual(s.list(), list);
});

test("applyDelta upserts, removes and expires", () => {
  const s = mk();
  s.replaceFull([{ id: "a", lon: 1, lat: 1 }, { id: "b", lon: 2, lat: 2 }], 0, [0, 0, 5, 5], null);
  assert.equal(s.applyDelta([{ id: "c", lon: 3, lat: 3 }], ["a"], 10_000), true);
  assert.deepEqual(s.list().map((r) => r.id).sort(), ["b", "c"]);
  assert.equal(s.applyDelta([], [], 65_000), true); // b (seen at 0) expired, c (seen at 10 s) kept
  assert.deepEqual(s.list().map((r) => r.id), ["c"]);
});

test("decode: geojson, compact columns, delta, density, truncation", () => {
  const g = decode<R>({ type: "FeatureCollection", truncated: true, total: 30000, version: 7, features: [{ geometry: { type: "Point", coordinates: [1, 2] }, properties: { id: "a" } }] }, false);
  assert.deepEqual(g.rows, [{ id: "a", lon: 1, lat: 2 }]);
  assert.equal(g.truncated, true);
  assert.equal(g.total, 30000);
  assert.equal(g.version, 7);
  const c = decode<R>({ type: "compact", cols: { id: ["a", "b"], lon: [1, 2], lat: [3, 4] }, removed: ["x"], version: 3 }, true);
  assert.equal(c.rows.length, 2);
  assert.deepEqual(c.rows[1], { id: "b", lon: 2, lat: 4 });
  assert.equal(c.delta, true);
  assert.deepEqual(c.removed, ["x"]);
  assert.equal(decode<R>({ type: "compact", cols: { lon: [], lat: [] }, removed: [] }, false).delta, false); // never asked for a delta
  assert.equal(decode<R>({ type: "density", cells: [{ h3: "x", count: 1, mil: 0 }] }, false).kind, "density");
  assert.match(truncationNote(20000, 51234), /20,000 of 51,234.*zoom in/);
});

test("cross-fade is continuous between zoom 4.5 and 5.5 and the two layers sum to one", () => {
  assert.equal(iconOpacity(4.4), 0);
  assert.equal(iconOpacity(5.6), 1);
  assert.equal(iconOpacity(5), 0.5);
  for (const z of [4.6, 4.9, 5.2]) assert.ok(Math.abs(iconOpacity(z) + densityOpacity(z) - 1) < 1e-9);
});

test("wanted() has hysteresis and keeps both datasets across the band", () => {
  let w = { points: false, density: true };
  w = wanted(w, 4.55);
  assert.deepEqual(w, { points: true, density: true });
  w = wanted(w, 4.4); // inside the hysteresis margin: still on
  assert.equal(w.points, true);
  w = wanted(w, 4.3);
  assert.deepEqual(w, { points: false, density: true });
  w = wanted({ points: true, density: true }, 5.6);
  assert.equal(w.density, true); // 5.5..5.65 still held
  w = wanted(w, 5.7);
  assert.deepEqual(w, { points: true, density: false });
  assert.ok(zoomParam("points", 4.7) >= 5 && zoomParam("density", 5.3) < 5);
});

// ---- Feed: sequence numbers, abort, no blanking on failure, since/compact detection
const rowsFC = (ids: string[], extra: object = {}) => ({
  type: "FeatureCollection",
  features: ids.map((id, i) => ({ geometry: { type: "Point", coordinates: [i + 1, 1] }, properties: { id } })),
  ...extra,
});
const feed = (f: (r: FeedReq) => Promise<unknown>, t = { now: 0 }) => new Feed<R>("points", (r) => r.id, 60_000, f, () => t.now);
const VIEW: Box = [0, 0, 10, 10];

test("a stale response is dropped and the superseded request is aborted", async () => {
  const resolvers: ((v: unknown) => void)[] = [];
  const signals: AbortSignal[] = [];
  const fd = feed((r) => new Promise((res) => { signals.push(r.signal); resolvers.push(res); }));
  const p1 = fd.poll(VIEW, 6, "");
  const p2 = fd.poll([50, 50, 60, 60], 6, ""); // a different view supersedes the first
  assert.equal(signals[0]!.aborted, true);
  resolvers[1]!(rowsFC(["new"]));
  assert.equal(await p2, true);
  resolvers[0]!(rowsFC(["old"])); // arrives late
  assert.equal(await p1, false);
  assert.deepEqual(fd.store.list().map((r) => r.id), ["new"]);
});

test("the same request in flight is not duplicated", () => {
  let n = 0;
  const fd = feed(() => new Promise(() => void n++));
  void fd.poll(VIEW, 6, "");
  void fd.poll(VIEW, 6, "");
  assert.equal(n, 1);
});

test("a failed request keeps the last picture", async () => {
  let fail = false;
  const fd = feed(async () => { if (fail) throw new Error("boom"); return rowsFC(["a", "b"]); });
  await fd.poll(VIEW, 6, "");
  fail = true;
  await fd.poll(VIEW, 6, "");
  assert.equal(fd.store.size, 2);
});

test("while the view stays inside the fetched box the same padded box is reused; leaving it refetches", async () => {
  const boxes: string[] = [];
  const fd = feed(async (r) => { boxes.push(r.bbox); return rowsFC(["a"]); });
  await fd.poll(VIEW, 6, "");
  await fd.poll([2, 2, 8, 8], 6, "");
  assert.equal(boxes[0], boxes[1]);
  await fd.poll([40, 40, 50, 50], 6, "");
  assert.notEqual(boxes[2], boxes[0]);
});

test("since is sent only after the server reported a version; compact falls back to GeoJSON", async () => {
  const seen: (number | undefined)[] = [];
  const compactAsked: boolean[] = [];
  let version = 1;
  const fd = feed(async (r) => {
    seen.push(r.since);
    compactAsked.push(r.compact);
    return r.since === undefined ? rowsFC(["a", "b"], { version }) : { ...rowsFC(["c"]), removed: ["a"], version: ++version };
  });
  await fd.poll(VIEW, 6, "");
  await fd.poll(VIEW, 6, "");
  assert.deepEqual(seen, [undefined, 1]);
  assert.deepEqual(compactAsked, [true, false]); // the first answer was not columnar
  assert.deepEqual(fd.store.list().map((r) => r.id).sort(), ["b", "c"]);
});

test("an old server without version or truncated fields still works", async () => {
  const fd = feed(async () => rowsFC(["a"]));
  await fd.poll(VIEW, 6, "");
  await fd.poll(VIEW, 6, "");
  assert.equal(fd.store.size, 1);
  assert.equal(fd.truncated, false);
});
