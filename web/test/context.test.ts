import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { installFakeDom } from "./fakedom.ts";

installFakeDom();
const { parseContext, renderContext, effect, gloss, EMPTY_WEATHER, EMPTY_IMAGERY } = await import("../src/context.ts");
const load = (n: string) => parseContext(JSON.parse(readFileSync(new URL(`../src/__fixtures__/context_${n}.json`, import.meta.url), "utf8")));
const dump = (el: unknown): string => (el as { dump(): string }).dump();

test("confounders present: notes, confidence effect, footprints, three passes with UTC and local", () => {
  const text = dump(renderContext(load("confounders"), { tz: "Asia/Yangon" }));
  assert.match(text, /Things that could explain this/);
  assert.match(text, /Cloud cover 85%/);
  assert.match(text, /confidence lowered ×0\.8/);
  assert.match(text, /geomagnetic storm \(Kp 6\)/);
  assert.match(text, /Can this be checked\?/);
  assert.match(text, /sentinel-2b · 2026-09-29 04:00 UTC · cloud 38%/);
  assert.match(text, /radar, sees through cloud/);
  for (const sat of ["SENTINEL-2A (MSI)", "SENTINEL-1A (C-SAR)", "SENTINEL-2B (MSI)"]) assert.ok(text.includes(sat));
  assert.match(text, /2026-10-01 03:30 UTC · 01 Oct, 10:00 local/);
});

test("none: honest empty states, no alarming words", () => {
  const text = dump(renderContext(load("none")));
  assert.ok(text.includes(EMPTY_WEATHER));
  assert.ok(text.includes(EMPTY_IMAGERY));
  assert.match(text, /Not checked yet/);
  assert.doesNotMatch(text, /false|refuted/i);
});

test("gaps only: gaps listed, empty states still shown", () => {
  const text = dump(renderContext(load("gaps")));
  assert.match(text, /Only 2 of 3 aggregators/);
  assert.ok(text.includes(EMPTY_WEATHER));
});

test("not loaded yet says not checked, never false", () => {
  const text = dump(renderContext(null));
  assert.match(text, /not checked yet/);
});

test("helpers", () => {
  assert.equal(effect({ note: "", factor: null }), "confidence lowered");
  assert.equal(gloss("Kp 5"), "geomagnetic storm (Kp 5)");
  assert.equal(gloss("geomagnetic storm (Kp 6)"), "geomagnetic storm (Kp 6)");
  assert.equal(parseContext("garbage").confounders.length, 0);
});

test("footprint link callback fires with the feature id; link text is not an anchor", () => {
  let got = "";
  const el = renderContext(load("confounders"), { onFootprint: (id) => (got = id) }) as never as { children: unknown[]; dump(): string };
  const find = (n: { tag?: string; attrs?: Record<string, string>; children: unknown[] }): { fire(t: string): void } | null => {
    if (n.tag === "button" && n.attrs?.class === "ctx-link") return n as never;
    for (const c of n.children as never[]) {
      const r = find(c);
      if (r) return r;
    }
    return null;
  };
  find(el as never)!.fire("click");
  assert.equal(got, "stac|s2");
  assert.doesNotMatch(readFileSync(new URL("../src/context.ts", import.meta.url), "utf8"), /innerHTML|"a"|window\.open|location/);
});
