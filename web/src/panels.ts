// Shared UI helpers, alert queue and data-health view.
import { fmtDT, fmtHM, tzLabel } from "./tz";
import type { Alert } from "./api";
import { ago, fmtTime, h } from "./dom";
import { css } from "./theme/palette";
import { explainBlock } from "./explain";
import { alertTitle, aoiName, kindLabel, PRIORITY_WORD, SOURCE_LABEL, T } from "./labels";
import { api } from "./api";
import { patch, refreshAll, selectAlert, state, triage } from "./store";
import { filters as mapFilters } from "./filters";
import { inBand } from "./spectrummodel";

const SEV_RANK: Record<string, number> = { critical: 4, high: 3, medium: 2, low: 1, info: 0 };
export const priority = (a: Alert): number => SEV_RANK[a.severity]! * a.confidence;

/** Two alert categories in the UI: Critical (critical + high severity) and Warning (the rest).
 * Severity stays five-level in the data (detectors, fusion, ordering); icon + text, never colour alone. */
export const PRIORITY: Record<string, { p: string; icon: string; label: string }> = {
  critical: { p: "P1", icon: "▲", label: "Critical" },
  high: { p: "P1", icon: "▲", label: "Critical" },
  medium: { p: "P2", icon: "●", label: "Warning" },
  low: { p: "P2", icon: "●", label: "Warning" },
  info: { p: "P2", icon: "●", label: "Warning" },
};
export const isCritical = (a: { severity: string }): boolean => PRIORITY[a.severity]?.p === "P1";

export const STATUS_LABEL: Record<string, string> = {
  new: "New",
  acknowledged: "Acknowledged",
  under_review: "Under review",
  shelved: "Shelved",
  confirmed: "Confirmed",
  dismissed: "Dismissed",
  expired: "Expired",
};

/** Responsive table: scrolls inside its own wrapper; stacked label/value rows at <768px. */
export function table(headers: string[], rows: (Node | string)[][]): HTMLElement {
  return h(
    "div",
    { class: "tw" },
    h(
      "table",
      { class: "stack" },
      h("thead", {}, h("tr", {}, ...headers.map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((c, i) => h("td", { "data-label": headers[i] ?? "" }, c))))),
    ),
  );
}

export function chart(points: { ts: string; value: number }[], baseline: number | null, threshold: number | null): HTMLElement {
  const ns = "http://www.w3.org/2000/svg";
  const W = 360;
  const H = 190;
  const L = 40;
  const R = 8;
  const T = 8;
  const B = 26;
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "chart");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Share of aircraft affected over time, against the normal level and the alert line");
  const wrap = h("div", { class: "chartwrap" }, svg);
  if (!points.length) return wrap;
  const mk = (tag: string, attrs: Record<string, string>, text?: string): SVGElement => {
    const e = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    svg.append(e);
    return e as SVGElement;
  };
  const vals = points.map((p) => p.value);
  const max = Math.max(...vals, threshold ?? 0, baseline ?? 0, 0.01) * 1.1;
  const y = (v: number) => H - B - (v / max) * (H - B - T);
  const t0 = new Date(points[0]!.ts).getTime();
  const t1 = new Date(points[points.length - 1]!.ts).getTime();
  const x = (t: number) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i;
    mk("line", { x1: String(L), x2: String(W - R), y1: String(y(v)), y2: String(y(v)), class: "grid" });
    mk("text", { x: String(L - 4), y: String(y(v) + 4), "text-anchor": "end", class: "ax" }, `${(v * 100).toFixed(v * 100 < 10 ? 1 : 0)}%`);
  }
  for (let i = 0; i <= 3; i++) {
    const t = t0 + ((t1 - t0) / 3) * i;
    const d = new Date(t);
    const span = t1 - t0 > 36 * 3600_000;
    const lbl = span ? fmtDT(d).slice(0, 5) : fmtHM(d);
    mk("text", { x: String(x(t)), y: String(H - 8), "text-anchor": i === 0 ? "start" : i === 3 ? "end" : "middle", class: "ax" }, lbl);
  }
  if (baseline !== null) mk("path", { d: `M${L} ${y(baseline)} H${W - R}`, class: "ln base" });
  if (threshold !== null) mk("path", { d: `M${L} ${y(threshold)} H${W - R}`, class: "ln thr" });
  mk("path", { d: points.map((p, i) => `${i ? "L" : "M"}${x(new Date(p.ts).getTime())} ${y(p.value)}`).join(" "), class: "ln val" });
  wrap.append(
    h(
      "div",
      { class: "legend" },
      h("span", {}, h("i", { class: "k val" }), "aircraft affected"),
      baseline !== null && h("span", {}, h("i", { class: "k base" }), `normal ${(baseline * 100).toFixed(1)}%`),
      threshold !== null && h("span", {}, h("i", { class: "k thr" }), `alert line ${(threshold * 100).toFixed(1)}%`),
      h("span", { class: "note" }, `x: time (${tzLabel()}), y: % affected`),
    ),
  );
  return wrap;
}

export function sparkline(values: (number | null)[], baseline: number | null): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 100 28");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "24 hour trend versus baseline");
  const nums = values.filter((v): v is number => v !== null);
  if (!nums.length) return svg;
  const max = Math.max(...nums, baseline ?? 0, 0.02) * 1.15;
  const y = (v: number) => 26 - (v / max) * 24;
  const mk = (d: string, stroke: string, dash = "") => {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", stroke);
    p.setAttribute("stroke-width", "1.6");
    p.setAttribute("vector-effect", "non-scaling-stroke");
    if (dash) p.setAttribute("stroke-dasharray", dash);
    svg.append(p);
  };
  if (baseline !== null) mk(`M0 ${y(baseline)} H100`, css("notice"), "3 2");
  let d = "";
  values.forEach((v, i) => {
    if (v === null) return;
    d += `${d ? "L" : "M"}${(i / (values.length - 1)) * 100} ${y(v)} `;
  });
  mk(d, css("neutral"));
  return svg;
}

// ------------------------------------------------------------------ queue

const uniq = (xs: (string | null)[]): string[] => [...new Set(xs.filter((x): x is string => !!x))].sort();

export function visibleAlerts(): Alert[] {
  const f = state.filters;
  const w = state.window;
  const q = f.q.trim().toLowerCase();
  return state.alerts
    .filter(
      (a) =>
        (!f.aoi || a.aoi === f.aoi) &&
        inBand(a, mapFilters.band) &&
        (!f.kind || a.kind === f.kind) &&
        (!f.status || (f.status === "open" ? ["new", "acknowledged", "under_review"].includes(a.status) : a.status === f.status)) &&
        (!f.grade || a.admiralty.reliability === f.grade) &&
        (!f.priority || PRIORITY[a.severity]!.p === f.priority) &&
        (!q || `${alertTitle(a)} ${a.summary}`.toLowerCase().includes(q)) &&
        (!w || (new Date(a.first_seen).getTime() <= w.to && new Date(a.last_seen).getTime() >= w.from)),
    )
    .sort((a, b) => priority(b) - priority(a));
}

export function renderQueue(root: HTMLElement): void {
  const f = state.filters;
  const set = (k: keyof typeof f, v: string) => patch({ filters: { ...f, [k]: v } });
  const sel = (label: string, key: "aoi" | "kind" | "status" | "grade" | "priority", opts: string[], name: (v: string) => string = (v) => v) =>
    h(
      "select",
      { "aria-label": label, onchange: (e) => set(key, (e.target as HTMLSelectElement).value) },
      h("option", { value: "" }, `${label}: all`),
      ...opts.map((o) => h("option", { value: o, selected: f[key] === o }, name(o))),
    );
  const search = h("input", { type: "search", id: "q", placeholder: "Search ( / )", "aria-label": "Search alerts", value: f.q });
  search.addEventListener("input", () => {
    f.q = search.value; // no re-render while typing (keeps focus); queue refreshes on next event
    renderQueueList(list);
  });
  const list = h("div", {});
  const group = h(
    "select",
    { "aria-label": "Group by", onchange: (e) => set("group", (e.target as HTMLSelectElement).value) },
    ...(["none", "aoi", "priority"] as const).map((g) => h("option", { value: g, selected: f.group === g }, `Group: ${g === "aoi" ? "area" : g}`)),
  );
  root.replaceChildren(
    h("h2", { id: "qcount" }, T.alerts),
    search,
    h(
      "div",
      { class: "filters" },
      sel("Area", "aoi", uniq(state.alerts.map((a) => a.aoi)), aoiName),
      sel("Type", "kind", uniq(state.alerts.map((a) => a.kind)), kindLabel),
      sel("Status", "status", ["open", "new", "acknowledged", "under_review", "shelved", "confirmed", "dismissed", "expired"], (s) => STATUS_LABEL[s] ?? "Open"),
      sel("Source", "grade", ["A", "B", "C", "D", "E", "F"], (g) => SOURCE_LABEL[g] ?? g),
      sel("Category", "priority", ["P1", "P2"], (p) => PRIORITY_WORD[p] ?? p),
      group,
    ),
    h(
      "div",
      { class: "row" },
      h("button", { id: "bulk-ack", onclick: () => void bulkAck() }, "Acknowledge selected"),
    ),
    list,
    clearControl(root),
  );
  renderQueueList(list);
}

async function bulkAck(): Promise<void> {
  const ids = [...state.checked];
  for (const id of ids) await triage(id, "acknowledged");
  patch({ checked: new Set() });
}

function renderQueueList(list: HTMLElement): void {
  const shown = visibleAlerts();
  const h2 = document.getElementById("qcount");
  if (h2) h2.textContent = `${T.alerts} (${shown.length})`;
  const row = (a: Alert) => {
    const pr = PRIORITY[a.severity]!;
    const cb = h("input", {
      type: "checkbox",
      "aria-label": `Select ${alertTitle(a)}`,
      checked: state.checked.has(a.id),
      onchange: (e) => {
        const c = new Set(state.checked);
        if ((e.target as HTMLInputElement).checked) c.add(a.id);
        else c.delete(a.id);
        state.checked = c;
      },
    });
    return h(
      "div",
      { class: "qrow" },
      cb,
      h(
        "button",
        { class: `card sev-${a.severity}`, "data-id": a.id, "aria-pressed": String(a.id === state.selected), onclick: () => void selectAlert(a.id) },
        h("div", { class: "t" }, `${pr.icon} `, alertTitle(a)),
        explainBlock(a),
        h(
          "div",
          { class: "m" },
          h("span", {}, STATUS_LABEL[a.status] ?? a.status),
          h("span", { title: fmtTime(a.first_seen) }, ago(a.last_seen)),
        ),
      ),
    );
  };
  const nodes: Node[] = [];
  if (!shown.length) nodes.push(h("div", { class: "empty" }, T.noAlerts));
  else if (state.filters.group === "none") nodes.push(...shown.map(row));
  else {
    const key = (a: Alert) => (state.filters.group === "aoi" ? aoiName(a.aoi) : PRIORITY[a.severity]!.label);
    for (const g of uniq(shown.map(key)).sort()) {
      nodes.push(h("div", { class: "grp" }, g), ...shown.filter((a) => key(a) === g).map(row));
    }
  }
  list.replaceChildren(...nodes);
}

// Two-step in-page confirmation (no window.confirm). Clearing expires alerts; it is not a verdict.
let confirming = false;
let clearMsg = "";

function clearControl(root: HTMLElement): HTMLElement {
  const total = state.alerts.filter((a) => ["new", "acknowledged", "under_review", "shelved"].includes(a.status)).length;
  const redraw = (): void => renderQueue(root);
  if (!confirming) {
    return h(
      "div",
      { class: "clear-all" },
      h("button", { type: "button", class: "btn-clear", disabled: total === 0, onclick: () => { confirming = true; clearMsg = ""; redraw(); } }, `${T.clearAll} (${total})`),
      clearMsg && h("span", { class: "note", role: "status" }, clearMsg),
    );
  }
  return h(
    "div",
    { class: "clear-all confirm", role: "alertdialog", "aria-label": T.clearAll },
    h("span", {}, `Clear ${total} open alerts? They are marked expired, not dismissed, and the change is logged.`),
    h(
      "button",
      {
        type: "button",
        class: "btn-clear danger",
        onclick: () => {
          void api.clearAlerts().then(
            async (r) => {
              confirming = false;
              clearMsg = `Cleared ${r.cleared} alerts.`;
              await refreshAll();
              redraw();
            },
            () => {
              confirming = false;
              clearMsg = "Could not clear alerts.";
              redraw();
            },
          );
        },
      },
      "Yes, clear all",
    ),
    h("button", { type: "button", class: "btn-clear", onclick: () => { confirming = false; redraw(); } }, "Cancel"),
  );
}
