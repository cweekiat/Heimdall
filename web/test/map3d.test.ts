import assert from "node:assert/strict";
import { test } from "node:test";
import { installFakeDom } from "./fakedom.ts";

installFakeDom();
const m = await import("../src/map3d.ts");
const { BaseLayers } = await import("../src/basemap.ts");

const fakeClock = () => {
  let t = 0;
  const q: { at: number; cb: () => void }[] = [];
  return {
    clock: {
      now: () => t,
      frame: (cb: () => void) => void q.push({ at: t, cb }),
      later: (cb: () => void, ms: number) => void q.push({ at: t + ms, cb }),
    },
    advance(ms: number) {
      const end = t + ms;
      for (;;) {
        q.sort((a, b) => a.at - b.at);
        if (!q.length || q[0]!.at > end) break;
        const e = q.shift()!;
        t = Math.max(t, e.at);
        e.cb();
      }
      t = end;
    },
  };
};

test("default style is the local basemap and no external host is used", () => {
  assert.equal(m.OFFLINE_STYLE, "/basemap/style.json");
  const calls: unknown[][] = [];
  const map = { on() {}, setStyle: (...a: unknown[]) => calls.push(a), isStyleLoaded: () => true, getStyle: () => ({ layers: [], sources: {} }) };
  const bl = new BaseLayers(map as never, "openfreemap", ["borders"], "dark");
  assert.match(String(bl.initialStyle()), /^\/basemap\/style\.json/);
  bl.applyInitial();
  assert.match(String(calls[0]![0]), /^\/basemap\/style\.json/);
  assert.doesNotMatch(String(calls[0]![0]), /https?:/);
});

test("3D toggle sets pitch and bearing and back", () => {
  const eased: Record<string, number | undefined>[] = [];
  let rot = 0;
  const map = {
    easeTo: (o: Record<string, number | undefined>) => void eased.push(o),
    setMaxPitch() {},
    dragRotate: { enable: () => void rot++, disable() {} },
    touchZoomRotate: { enableRotation() {}, disableRotation() {} },
  };
  m.setViewMode(map, "3d");
  assert.equal(eased[0]!.pitch, 60);
  assert.equal(rot, 1);
  m.setViewMode(map, "2d");
  assert.equal(eased[1]!.pitch, 0);
  assert.equal(eased[1]!.bearing, 0);
});

test("altitude mapping", () => {
  assert.equal(m.altitudeMeters(10000, false, 2), 3048 * 2);
  assert.equal(m.altitudeMeters(null, false, 3), 0);
  assert.equal(m.altitudeMeters(5000, true, 3), 0);
});

test("drop lines only when enabled", () => {
  const rows = [
    { lon: 1, lat: 2, alt_baro: 1000 },
    { lon: 3, lat: 4, alt_baro: null },
  ];
  assert.equal(m.dropLines(rows, false, 1).length, 0);
  const d = m.dropLines(rows, true, 1);
  assert.equal(d.length, 1);
  assert.deepEqual(d[0]![0], [1, 2, 0]);
});

test("throttle: 50 updates in 1 s give at most 2 setData per layer; unused layer never added", () => {
  const fc = fakeClock();
  const added: string[] = [];
  const sets: string[] = [];
  const hub = new m.LayerHub<number>((id) => added.push(id), (id) => sets.push(id), fc.clock, 2);
  hub.setEnabled("air", true);
  for (let i = 0; i < 50; i++) {
    hub.update("air", i);
    hub.update("ships", i);
    fc.advance(19);
  }
  assert.deepEqual(added, ["air"]);
  assert.ok(sets.filter((s) => s === "air").length <= 2, String(sets.length));
  assert.ok(sets.filter((s) => s === "air").length >= 1);
  assert.equal(sets.filter((s) => s === "ships").length, 0);
  assert.equal(hub.isAdded("ships"), false);
});

test("unavailable basemap renders a hint without throwing", async () => {
  const st = await m.probeBasemap(async () => ({ ok: true, json: async () => ({ available: false, hint: "Run the fetch script." }) }));
  assert.equal(st.available, false);
  const el = m.unavailableHint(st.hint, { createElement: () => ({ className: "", textContent: "", setAttribute() {} }) });
  assert.match(String(el.textContent), /not installed/);
  const bad = await m.probeBasemap(async () => {
    throw new Error("down");
  });
  assert.equal(bad.available, false);
});
