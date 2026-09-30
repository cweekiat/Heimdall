// Timeline: stacked histogram of alerts by kind over the last 24 h, brush -> global time window,
// playback (1x = 1 min of data per second, 10x, 60x) and a "now" lock.
import { h } from "./dom";
import { css } from "./theme/palette";
import { kindLabel } from "./labels";
import { patch, state } from "./store";

const SPAN = 24 * 3600_000;
const BUCKETS = 48;
const W = 960;
const H = 76;
const PAD_B = 14;

// One hue (alert red); the alert types are told apart by stack order and the key's opacity steps.
const opacityFor = (kinds: string[], k: string): number => 1 - (kinds.indexOf(k) % 5) * 0.16;
const colorFor = (kinds: string[], k: string): string => css("alert", opacityFor(kinds, k));

let timer: number | null = null;
let collapsed = false;
let brushing: number | null = null; // pointer-down x in ms

const xToMs = (svg: SVGSVGElement, ev: PointerEvent, t0: number): number => {
  const r = svg.getBoundingClientRect();
  return t0 + Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)) * SPAN;
};

function stopPlay(): void {
  if (timer !== null) window.clearInterval(timer);
  timer = null;
  patch({ playing: 0 });
}

function play(speed: 1 | 10 | 60): void {
  if (timer !== null) window.clearInterval(timer);
  const now = Date.now();
  const w = state.window ?? { from: now - 2 * 3600_000, to: now };
  const width = Math.max(600_000, w.to - w.from);
  let from = state.window ? w.from : now - SPAN;
  patch({ playing: speed, window: { from, to: from + width } });
  timer = window.setInterval(() => {
    from += speed * 60_000 * 0.25;
    if (from + width >= Date.now()) {
      stopPlay();
      patch({ window: null });
      return;
    }
    patch({ window: { from, to: from + width } });
  }, 250);
}

export function togglePlay(): void {
  if (state.playing) stopPlay();
  else play(1);
}

export function toggleTimeline(root: HTMLElement): void {
  collapsed = !collapsed;
  root.classList.toggle("collapsed", collapsed);
}

export function renderTimeline(root: HTMLElement): void {
  if (brushing !== null) return;
  const t1 = Date.now();
  const t0 = t1 - SPAN;
  const alerts = state.alerts.filter((a) => state.filters.aoi === "" || a.aoi === state.filters.aoi);
  const kinds = [...new Set(alerts.map((a) => a.kind))].sort();
  const bw = SPAN / BUCKETS;
  const counts: Record<string, number>[] = Array.from({ length: BUCKETS }, () => ({}));
  for (const a of alerts) {
    const t = new Date(a.first_seen).getTime();
    if (t < t0 || t > t1) continue;
    const i = Math.min(BUCKETS - 1, Math.floor((t - t0) / bw));
    counts[i]![a.kind] = (counts[i]![a.kind] ?? 0) + 1;
  }
  const max = Math.max(1, ...counts.map((c) => Object.values(c).reduce((x, y) => x + y, 0)));
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Alerts opened per 30 minutes over the last 24 hours, by kind. Drag to select a time window.");
  const rect = (x: number, y: number, w: number, hh: number, fill: string, cls = "", title = ""): SVGRectElement => {
    const r = document.createElementNS(ns, "rect");
    r.setAttribute("x", String(x));
    r.setAttribute("y", String(y));
    r.setAttribute("width", String(Math.max(0, w)));
    r.setAttribute("height", String(Math.max(0, hh)));
    r.setAttribute("fill", fill);
    if (cls) r.setAttribute("class", cls);
    if (title) {
      const t = document.createElementNS(ns, "title");
      t.textContent = title;
      r.append(t);
    }
    svg.append(r);
    return r;
  };
  rect(0, 0, W, H - PAD_B, "transparent", "tl-bg");
  counts.forEach((c, i) => {
    let y = H - PAD_B;
    for (const k of kinds) {
      const n = c[k] ?? 0;
      if (!n) continue;
      const hh = (n / max) * (H - PAD_B - 4);
      y -= hh;
      rect((i / BUCKETS) * W + 1, y, W / BUCKETS - 2, hh, colorFor(kinds, k), "tl-bar", `${kindLabel(k)}: ${n} opened ${new Date(t0 + i * bw).toISOString().slice(11, 16)}Z`);
    }
  });
  for (let i = 0; i <= 6; i++) {
    const x = (i / 6) * W;
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", String(x));
    t.setAttribute("y", String(H - 3));
    t.setAttribute("text-anchor", i === 0 ? "start" : i === 6 ? "end" : "middle");
    t.setAttribute("class", "tl-ax");
    t.textContent = new Date(t0 + (SPAN / 6) * i).toISOString().slice(11, 16) + "Z";
    svg.append(t);
  }
  const w = state.window;
  const px = (ms: number): number => ((Math.min(t1, Math.max(t0, ms)) - t0) / SPAN) * W;
  const sel = rect(w ? px(w.from) : 0, 0, w ? px(w.to) - px(w.from) : 0, H - PAD_B, css("neutral", 0.22), "tl-sel");
  const setSel = (a: number, b: number): void => {
    sel.setAttribute("x", String(px(a)));
    sel.setAttribute("width", String(Math.max(0, px(b) - px(a))));
  };
  const el = svg as unknown as SVGSVGElement;
  let startMs = 0;
  svg.addEventListener("pointerdown", (e) => {
    stopPlay();
    svg.setPointerCapture(e.pointerId);
    startMs = xToMs(el, e, t0);
    brushing = startMs;
  });
  svg.addEventListener("pointermove", (e) => {
    if (brushing === null) return;
    const m = xToMs(el, e, t0);
    setSel(Math.min(startMs, m), Math.max(startMs, m));
  });
  svg.addEventListener("pointerup", (e) => {
    if (brushing === null) return;
    const m = xToMs(el, e, t0);
    brushing = null;
    if (Math.abs(m - startMs) < 5 * 60_000) patch({ window: null }); // click = clear
    else patch({ window: { from: Math.min(startMs, m), to: Math.max(startMs, m) } });
  });
  const label = state.window
    ? `${new Date(state.window.from).toISOString().slice(5, 16).replace("T", " ")}Z to ${new Date(state.window.to).toISOString().slice(5, 16).replace("T", " ")}Z`
    : "now (live)";
  const sp = (n: 1 | 10 | 60) => h("button", { "aria-pressed": String(state.playing === n), onclick: () => play(n) }, `${n}×`);
  root.replaceChildren(
    h(
      "div",
      { class: "tl-head" },
      h("button", { onclick: togglePlay, "aria-label": state.playing ? "Pause" : "Play" }, state.playing ? "Pause" : "Play"),
      sp(1),
      sp(10),
      sp(60),
      h("button", { "aria-pressed": String(state.window === null), onclick: () => { stopPlay(); patch({ window: null }); } }, "Now"),
      h("span", { class: "note" }, `Showing: ${label}`),
      h("span", { class: "note tl-key" }, ...kinds.map((k) => h("span", {}, h("i", { class: "sw", style: `background:${colorFor(kinds, k)}` }), kindLabel(k)))),
    ),
    h("div", { class: "tl-body" }, svg),
  );
}
