import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { dashSegments, hoverText, imageryLagDays, lightsVsUsual, nextPass, parseLayer, thermalRadius, TRUNCATED_NOTICE } from "../src/p3model.ts";

const load = (n: string) => parseLayer(JSON.parse(readFileSync(new URL(`../src/__fixtures__/layers_${n}.json`, import.meta.url), "utf8")));
const NOW = Date.parse("2026-09-30T12:00:00Z");

test("parse drops malformed features and keeps ids", () => {
  const fc = load("thermal");
  assert.equal(fc.features.length, 3);
  assert.equal(fc.truncated, false);
  assert.ok(fc.features[0]!.id.startsWith("firms|"));
});

test("heat spot radius grows with power on a log scale and stays bounded", () => {
  assert.ok(thermalRadius(3) < thermalRadius(42));
  assert.ok(thermalRadius(1e9) <= 18);
  assert.ok(thermalRadius(null) >= 4);
});

test("hover text per layer", () => {
  const t = load("thermal").features[0]!;
  assert.match(hoverText("thermal", t), /N20.*42.5 MW.*confidence high/);
  assert.match(hoverText("outage", load("outage").features[0]!), /MM-MDY.*42% of normal/);
  const [s1, s2] = load("imagery").features;
  assert.match(hoverText("imagery", s1!), /cloud n\/a \(radar\)/);
  assert.match(hoverText("imagery", s2!), /cloud 38%/);
});

test("night lights: ratio to median when present, radiance otherwise; lag is shown", () => {
  const [a, b] = load("night_lights").features;
  assert.equal(lightsVsUsual(a!.properties), 0.35);
  assert.equal(lightsVsUsual(b!.properties), null);
  assert.match(hoverText("night_lights", a!, NOW), /35% of the 30-night median.*imagery lag 4 days/);
  assert.match(hoverText("night_lights", b!, NOW), /4 nW/);
  assert.equal(imageryLagDays(a!.properties, NOW), 4);
});

test("truncated flag and notice", () => {
  assert.equal(load("imagery").truncated, true);
  assert.match(TRUNCATED_NOTICE, /Zoom in or shorten/);
});

test("next pass picks the soonest future pass", () => {
  const c = nextPass("myanmar", load("overpass"), NOW)!;
  assert.match(c.text, /SENTINEL-1A/);
  assert.equal(nextPass("x", load("overpass"), Date.parse("2027-01-01T00:00:00Z")), null);
});

test("dashed outline has gaps", () => {
  const ring = load("imagery").features[0]!.geometry!.coordinates as number[][][];
  const segs = dashSegments(ring[0]!);
  assert.ok(segs.length > 0 && segs.length < 32);
});

test("no innerHTML / img / fetch of image hosts in P3 sources", () => {
  for (const f of ["p3layers.ts", "p3model.ts"]) {
    const src = readFileSync(new URL(`../src/${f}`, import.meta.url), "utf8");
    assert.doesNotMatch(src, /innerHTML|<img|new Image|insertAdjacentHTML/);
  }
});
