import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { PALETTE, severityStyle, rgba } from "../src/theme/palette.ts";

test("palette exports at most 5 semantic tokens", () => {
  assert.ok(Object.keys(PALETTE).length <= 5);
});

test("no hex or rgb literals in web/src outside theme/", () => {
  const bad: string[] = [];
  const walk = (d: string): void => {
    for (const f of readdirSync(d)) {
      const p = join(d, f);
      if (statSync(p).isDirectory()) {
        if (f !== "theme") walk(p);
      } else if (/\.ts$/.test(f)) {
        const src = readFileSync(p, "utf8");
        if (/#[0-9a-fA-F]{3,8}\b|\brgba?\s*\(\s*\d/.test(src.replace(/\/\/.*$/gm, ""))) bad.push(p);
      }
    }
  };
  walk(fileURLToPath(new URL("../src", import.meta.url)));
  assert.deepEqual(bad, []);
});

test("severity changes size and opacity but never colour", () => {
  const sevs = ["info", "low", "medium", "high", "critical"] as const;
  const st = sevs.map(severityStyle);
  assert.ok(st.every((s) => s.token === "alert"));
  for (let i = 1; i < st.length; i++) {
    assert.ok(st[i]!.radius > st[i - 1]!.radius);
    assert.ok(st[i]!.opacity > st[i - 1]!.opacity);
  }
  const cols = new Set(sevs.map((s) => rgba("alert", severityStyle(s).opacity).slice(0, 3).join()));
  assert.equal(cols.size, 1);
});
