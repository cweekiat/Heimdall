import assert from "node:assert/strict";
import { test } from "node:test";
const g = await import("../src/aogeo.ts");

test("circle ring is closed and every point is at the radius", () => {
  const ring = g.circleRing(1.2, 103.8, 40);
  assert.deepEqual(ring[0]!.map((v) => +v.toFixed(6)), ring[ring.length - 1]!.map((v) => +v.toFixed(6)));
  for (const p of ring) assert.ok(Math.abs(g.distNm([103.8, 1.2], p) - 40) < 0.05);
});

test("distNm: one degree of latitude is 60 nm", () => {
  assert.ok(Math.abs(g.distNm([0, 0], [0, 1]) - 60.04) < 0.1);
});

test("name rules mirror the server", () => {
  assert.equal(g.nameProblem("Harbour Watch_1-a"), null);
  assert.ok(g.nameProblem(""));
  assert.ok(g.nameProblem("   "));
  assert.ok(g.nameProblem("x".repeat(41)));
  assert.equal(g.nameProblem("x".repeat(40)), null);
  for (const bad of ["<b>x</b>", "a;b", "a/b", "é", " lead"]) assert.ok(g.nameProblem(bad), bad);
});

test("circle radius limits 5..500 nm", () => {
  assert.ok(g.circleProblem(0, 0, 4.9));
  assert.equal(g.circleProblem(0, 0, 5), null);
  assert.equal(g.circleProblem(0, 0, 500), null);
  assert.ok(g.circleProblem(0, 0, 500.1));
  assert.ok(g.circleProblem(91, 0, 50));
  assert.ok(g.circleProblem(0, Number.NaN, 50));
});

test("polygon limits: vertices, span, degenerate, range", () => {
  const box: [number, number][] = [[10, 110], [10, 112], [12, 112], [12, 110]];
  assert.equal(g.polygonProblem(box), null);
  assert.ok(g.polygonProblem(box.slice(0, 2)));
  assert.ok(g.polygonProblem([[0, 0], [1, 1], [2, 2]]));
  assert.ok(g.polygonProblem([[0, 0], [0, 10.5], [1, 5]]));
  assert.equal(g.polygonProblem([[0, 0], [0, 10], [1, 5]]), null);
  assert.ok(g.polygonProblem([[0, 0], [95, 1], [1, 2]]));
  const many = Array.from({ length: 201 }, (_, i): [number, number] => [Math.sin(i) , Math.cos(i)]);
  assert.ok(g.polygonProblem(many));
});

test("span and polygon ring", () => {
  assert.deepEqual(g.span([[1, 2], [4, 9], [3, 5]]), { lat: 3, lon: 7 });
  const r = g.polygonRing([[1, 2], [3, 4], [5, 0]]);
  assert.deepEqual(r, [[2, 1], [4, 3], [0, 5], [2, 1]]);
});

test("point in polygon handles a concave notch", () => {
  const c: [number, number][] = [[0, 0], [0, 4], [2, 4], [2, 2], [4, 2], [4, 0]];
  assert.ok(g.pointInPolygon([1, 1], c));
  assert.ok(!g.pointInPolygon([3.5, 3.5], c));
  assert.ok(!g.pointInPolygon([-1, 1], c));
});

test("double-click duplicate point is dropped; clicks convert to lat/lon", () => {
  const pts: [number, number][] = [[1, 2], [3, 4], [3, 4], [3, 4]];
  assert.deepEqual(g.dedupeTail(pts), [[1, 2], [3, 4]]);
  assert.deepEqual(g.toLatLon([[103.123456789, 1.987654321]]), [[1.98765, 103.12346]]);
});
