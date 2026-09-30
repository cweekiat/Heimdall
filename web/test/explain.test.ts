import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { installFakeDom } from "./fakedom.ts";

installFakeDom();
const { explainBlock, plainSummary } = await import("../src/explain.ts");
const { KeysSection, KEY_BANNER } = await import("../src/keysui.ts");
const fx = JSON.parse(readFileSync(new URL("./fixtures.json", import.meta.url), "utf8"));

test("area card shows ref, every source with grade, and the plain sentence", () => {
  const { alert, evidence } = fx.area;
  const text = (explainBlock(alert, evidence) as never as { dump(): string }).dump();
  assert.match(text, /HMD-20260930-0007/);
  for (const s of alert.sources) assert.ok(text.includes(s.connector) && text.includes(s.family));
  assert.match(text, /B2/);
  assert.match(text, /C3/);
  assert.equal(
    plainSummary(alert, evidence),
    "Area ~52 km where GPS jamming detected between 14:10–15:40 UTC, 37 aircraft affected",
  );
});

test("single-track card says ship, id and time window", () => {
  const { alert, evidence } = fx.track;
  const text = (explainBlock(alert, evidence) as never as { dump(): string }).dump();
  assert.match(text, /HMD-20260930-0008/);
  assert.match(text, /aisstream/);
  assert.match(text, /B3/);
  assert.equal(plainSummary(alert, evidence), "Ship 5630xxxxx reported impossible positions between 09:00–09:30 UTC");
});

test("keys form: PUT gets the value, input cleared, value absent from DOM and storage, banner shown", async () => {
  const store: Record<string, string> = {};
  (globalThis as unknown as { localStorage: unknown }).localStorage = {
    setItem: (k: string, v: string) => (store[k] = v),
    getItem: (k: string) => store[k] ?? null,
  };
  const TYPED = "test-value-ZZZZ1234";
  let rows = [{ name: "AISSTREAM_KEY", connector: "aisstream", is_set: false, last4: null, origin: null }];
  const puts: [string, string][] = [];
  const sec = new KeysSection({
    list: async () => rows as never,
    put: async (n: string, v: string) => {
      puts.push([n, v]);
      rows = [{ name: n, connector: "aisstream", is_set: true, last4: "1234", origin: "store" }];
    },
    del: async () => undefined,
  });
  await sec.load();
  const root = sec.el as never as import("./fakedom.ts").FakeNode;
  root.find((n) => n.tag === "button" && n.children[0]?.textContent === "Set")!.fire("click");
  const input = root.find((n) => n.tag === "input")!;
  assert.equal(input.attrs.type, "password");
  assert.equal(input.attrs.autocomplete, "off");
  input.value = TYPED;
  root.find((n) => n.tag === "form")!.fire("submit");
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(puts, [["AISSTREAM_KEY", TYPED]]);
  assert.equal(input.value, "");
  const dom = root.dump();
  assert.ok(!dom.includes(TYPED));
  assert.ok(!JSON.stringify(store).includes(TYPED));
  assert.ok(dom.includes(KEY_BANNER));
  assert.ok(dom.includes("••••1234"));
  assert.ok(!dom.includes("value-ZZZZ"));
});
