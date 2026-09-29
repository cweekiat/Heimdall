import "./style.css";
import { api } from "./api";
import type { Alert } from "./api";
import { renderBoard } from "./board";
import { ago, h } from "./dom";
import { renderEntity, renderInspector } from "./inspector";
import { alertTitle, aoiName, setAois } from "./labels";
import type { OpsMap } from "./map";
import { PRIORITY, renderHealth, renderQueue, visibleAlerts } from "./panels";
import { patch, refreshAll, refreshHealth, selectAlert, selectAoi, state, subscribe, triage, upsertAlert } from "./store";

const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;
const mobile = (): boolean => matchMedia("(max-width: 1100px)").matches;

// ---- theme: dark -> light -> high contrast
const THEMES = ["dark", "light", "hc"] as const;
type Theme = (typeof THEMES)[number];
const stored = localStorage.getItem("theme") as Theme | null;
let theme: Theme = stored && THEMES.includes(stored) ? stored : matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
document.documentElement.dataset.theme = theme;
let ops: OpsMap | null = null;
$("theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]!;
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("theme", theme);
  ops?.setTheme(theme === "light" ? "light" : "dark");
});
$("rail-toggle").addEventListener("click", () => document.body.classList.toggle("rail-collapsed"));

// ---- panels / tabs (mobile shows one at a time; desktop is 3-pane)
function showPanel(p: string): void {
  document.body.dataset.panel = p;
  document.querySelectorAll<HTMLButtonElement>("#tabs button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.panel === p)));
  if (p === "health") void refreshHealth();
}
document.querySelectorAll<HTMLButtonElement>("#tabs button").forEach((b) => b.addEventListener("click", () => showPanel(b.dataset.panel!)));
showPanel(matchMedia("(max-width: 720px)").matches ? "queue" : "map");

// ---- map controls
const slider = h("input", { type: "range", min: "0", max: "24", step: "1", value: "0", "aria-label": "Time slider, hours before now" });
const sliderLbl = h("span", {}, "now");
slider.addEventListener("input", () => {
  if (ops) ops.state.hoursAgo = Number(slider.value);
  sliderLbl.textContent = slider.value === "0" ? "now" : `-${slider.value} h`;
  ops?.render();
});
const cov = h("input", { type: "checkbox", id: "cov" });
cov.addEventListener("change", () => {
  if (ops) ops.state.showCoverage = cov.checked;
  ops?.render();
});
$("mapctl").replaceChildren(h("label", { for: "cov" }, cov, "Coverage (quality aircraft)"), h("label", {}, "Time ", slider, sliderLbl));

// ---- status bar
function renderStatus(): void {
  $("clock").textContent = new Date().toISOString().slice(0, 19).replace("T", " ") + "Z";
}
setInterval(renderStatus, 1000);
renderStatus();

const isOpenA = (a: Alert): boolean => ["new", "acknowledged", "under_review"].includes(a.status);

function renderChips(): void {
  $("sb-chips").replaceChildren(
    ...state.summary.map((s) =>
      h(
        "button",
        { class: `chip sev-${s.max_severity ?? "info"}`, "aria-pressed": String(state.selectedAoi === s.name), title: `${aoiName(s.name)}: ${s.open_total} open`, onclick: () => selectAoi(s.name) },
        `${s.max_severity ? PRIORITY[s.max_severity]!.icon : "○"} ${aoiName(s.name)} ${s.open_total}`,
      ),
    ),
  );
  const open = state.alerts.filter(isOpenA);
  const counts: Record<string, number> = { P1: 0, P2: 0, P3: 0, P4: 0 };
  open.forEach((a) => (counts[PRIORITY[a.severity]!.p]! += 1));
  $("sb-counts").replaceChildren(
    ...Object.entries(counts).map(([p, n]) =>
      h(
        "button",
        {
          class: `pill sev-${p === "P1" ? "critical" : p === "P2" ? "high" : p === "P3" ? "medium" : "low"}`,
          "aria-pressed": String(state.filters.priority === p),
          title: `Filter to ${p} (open alerts)`,
          onclick: () => patch({ filters: { ...state.filters, priority: state.filters.priority === p ? "" : p, status: state.filters.priority === p ? state.filters.status : "open" } }),
        },
        `${p} ${n}`,
      ),
    ),
  );
  const dom: Record<string, "ok" | "warn" | "bad"> = {};
  const rank = { ok: 0, warn: 1, bad: 2 } as const;
  for (const s of state.health) {
    const age = s.last_ok ? (Date.now() - new Date(s.last_ok).getTime()) / 1000 : Infinity;
    const g = age < s.expected_latency_s * 2 ? "ok" : age < s.expected_latency_s * 5 ? "warn" : "bad";
    if (!dom[s.domain] || rank[g] > rank[dom[s.domain]!]) dom[s.domain] = g;
  }
  $("sb-fresh").replaceChildren(
    ...Object.entries(dom).map(([d, g]) => h("span", { title: `${d}: ${g === "ok" ? "fresh" : g === "warn" ? "delayed" : "stale"}` }, h("span", { class: `dot ${g}` }), d)),
  );
}

// Critical banner: persistent, never auto-jumps the view.
function renderBanner(): void {
  const b = $("banner");
  const crit = state.alerts.filter((a) => a.severity === "critical" && a.status === "new").sort((x, y) => y.last_seen.localeCompare(x.last_seen));
  const a = crit[0];
  if (!a) {
    b.hidden = true;
    return;
  }
  b.hidden = false;
  b.replaceChildren(
    h("strong", {}, `▲▲ P1 ${alertTitle(a)}`),
    ` · ${aoiName(a.aoi)} · ${ago(a.first_seen)}`,
    crit.length > 1 ? ` · +${crit.length - 1} more` : "",
    h("button", { onclick: () => { void selectAlert(a.id); if (mobile()) showPanel("detail"); } }, "Go to"),
    h("button", { onclick: () => void triage(a.id, "acknowledged") }, "Ack"),
  );
}

// ---- linked rendering
function draw(changed: Set<string>): void {
  const has = (...k: string[]): boolean => k.some((x) => changed.has(x));
  if (has("alerts", "filters", "selected", "checked", "window")) {
    if (!(document.activeElement instanceof HTMLInputElement && document.activeElement.id === "q")) renderQueue($("queue"));
    else document.getElementById("qcount") && (document.getElementById("qcount")!.textContent = `Alerts (${visibleAlerts().length})`);
  }
  if (has("summary", "selectedAoi")) renderBoard($("board"));
  if (has("alerts", "summary", "health", "filters", "selectedAoi")) {
    renderChips();
    renderBanner();
  }
  if (has("detail", "tab", "selected", "selectedEntity", "tracks")) {
    if (state.selectedEntity) renderEntity($("inspector"), state.selectedEntity);
    else renderInspector($("inspector"));
    document.body.classList.toggle("has-selection", state.selected !== null);
  }
  if (has("health") && document.body.dataset.panel === "health") renderHealth($("health"), state.health);
  if (ops && has("alerts", "cells", "aois", "selected", "detail", "tracks", "filters", "window")) {
    Object.assign(ops.state, {
      alerts: visibleAlerts(),
      cells: state.cells,
      aois: state.aois,
      selected: state.selected,
      evidence: state.detail?.evidence ?? [],
      tracks: state.tracks,
    });
    ops.render();
  }
  if (ops && has("fly") && state.fly) ops.flyTo(state.fly.lon, state.fly.lat);
  if (has("selected") && state.selected && mobile()) showPanel("detail");
}
subscribe(draw);

// ---- live updates (SSE) with polling fallback
function live(): void {
  const es = new EventSource("/api/stream");
  const pill = $("live");
  es.onopen = () => {
    pill.textContent = "live";
    pill.classList.add("on");
  };
  es.addEventListener("snapshot", (e) => patch({ alerts: JSON.parse((e as MessageEvent<string>).data) as Alert[] }));
  es.addEventListener("alert", (e) => upsertAlert(JSON.parse((e as MessageEvent<string>).data) as Alert));
  es.onerror = () => {
    pill.textContent = "reconnecting";
    pill.classList.remove("on");
  };
}

async function boot(): Promise<void> {
  try {
    const aois = await api.aois();
    setAois(aois);
    patch({ aois });
    const [{ OpsMap: OpsMapCls, legend }, ui] = await Promise.all([import("./map"), api.uiConfig()]);
    ops = new OpsMapCls($("map"), theme === "light" ? "light" : "dark", ui.basemap, ui.overlays, (id) => void selectAlert(id, false));
    $("legend").replaceChildren(legend());
    mountLayersDrawer($("layers"), ops.layers.panel());
    await Promise.all([refreshAll(), refreshHealth().catch(() => undefined)]);
    patch({ aois });
  } catch (e) {
    $("queue").replaceChildren(h("div", { class: "empty" }, `API unavailable: ${String(e)}`));
  }
  live();
  setInterval(() => void refreshAll().catch(() => undefined), 60_000);
  setInterval(() => void refreshHealth().catch(() => undefined), 60_000);
}
void boot();

/** Collapsible layers drawer: closed by default, closes on outside click / Escape. */
function mountLayersDrawer(root: HTMLElement, body: HTMLElement): void {
  const btn = h("button", { "aria-expanded": "false", "aria-controls": "layers-drawer" }, "Layers");
  const close = h("button", { class: "drawer-close", "aria-label": "Close layers" }, "Close");
  const drawer = h("div", { class: "drawer", id: "layers-drawer" }, close, body);
  const set = (open: boolean): void => {
    drawer.hidden = !open;
    document.body.classList.toggle("layers-open", open);
    btn.setAttribute("aria-expanded", String(open));
  };
  btn.addEventListener("click", () => set(Boolean(drawer.hidden)));
  close.addEventListener("click", () => set(false));
  document.addEventListener("pointerdown", (e) => {
    if (!drawer.hidden && !root.contains(e.target as Node)) set(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") set(false);
  });
  root.replaceChildren(btn, drawer);
  set(false);
}
