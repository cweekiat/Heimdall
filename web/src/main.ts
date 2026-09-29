import "./style.css";
import { api } from "./api";
import type { Alert, Aoi, Cell, Evidence, TrackPt } from "./api";
import { h } from "./dom";
import type { OpsMap } from "./map";
import { emptyFilters, renderDetail, renderHealth, renderQueue } from "./panels";

const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;

let alerts: Alert[] = [];
let cells: Cell[] = [];
let aois: Aoi[] = [];
let selected: string | null = null;
const filters = emptyFilters();

// ---- theme
const storedTheme = localStorage.getItem("theme");
const theme0: "dark" | "light" =
  storedTheme === "light" || (!storedTheme && matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark";
document.documentElement.dataset.theme = theme0;

let ops: OpsMap | null = null;

$("theme").addEventListener("click", () => {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  localStorage.setItem("theme", t);
  ops?.setTheme(t);
});

// ---- panels / tabs
function showPanel(p: string): void {
  document.body.dataset.panel = p;
  document.querySelectorAll<HTMLButtonElement>("#tabs button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.panel === p)));
  if (p === "health") void loadHealth();
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
$("mapctl").replaceChildren(
  h("label", { for: "cov" }, cov, "Coverage (quality aircraft)"),
  h("label", {}, "Time ", slider, sliderLbl),
);

function pushMap(evidence: Evidence[] = [], tracks: { hex: string; path: TrackPt[] }[] = []): void {
  if (!ops) return;
  Object.assign(ops.state, { alerts, cells, aois, selected, evidence, tracks });
  ops?.render();
}

function drawQueue(): void {
  renderQueue($("queue"), alerts, filters, selected, (id) => void select(id, false), drawQueue);
}

async function select(id: string, fromMap: boolean): Promise<void> {
  selected = id;
  drawQueue();
  const d = await api.alert(id);
  renderDetail($("detail"), d, () => void select(id, false).then(refresh));
  const tracks = (
    await Promise.all(
      [...new Set(d.evidence.flatMap((e) => e.entities))].map(async (hex) => ({
        hex,
        path: await api.track(hex).catch(() => [] as TrackPt[]),
      })),
    )
  ).filter((t) => t.path.length > 1);
  pushMap(d.evidence, tracks);
  if (d.alert.lat !== null && d.alert.lon !== null) ops?.flyTo(d.alert.lon, d.alert.lat);
  if (!fromMap || matchMedia("(max-width: 1100px)").matches) showPanel(matchMedia("(max-width: 1100px)").matches ? "detail" : document.body.dataset.panel!);
}

async function refresh(): Promise<void> {
  [alerts, cells] = await Promise.all([api.alerts(), api.cells()]);
  drawQueue();
  pushMap(ops?.state.evidence, ops?.state.tracks);
}

async function loadHealth(): Promise<void> {
  renderHealth($("health"), (await api.health()).sources);
}

// ---- live updates (SSE) with polling fallback
function live(): void {
  const es = new EventSource("/api/stream");
  const pill = $("live");
  es.onopen = () => {
    pill.textContent = "live";
    pill.classList.add("on");
  };
  es.addEventListener("snapshot", (e) => {
    alerts = JSON.parse((e as MessageEvent<string>).data) as Alert[];
    drawQueue();
    pushMap(ops?.state.evidence, ops?.state.tracks);
  });
  es.addEventListener("alert", (e) => {
    const a = JSON.parse((e as MessageEvent<string>).data) as Alert;
    alerts = [a, ...alerts.filter((x) => x.id !== a.id)];
    drawQueue();
    pushMap(ops?.state.evidence, ops?.state.tracks);
    if (a.id === selected) void select(a.id, false);
  });
  es.onerror = () => {
    pill.textContent = "reconnecting";
    pill.classList.remove("on");
  };
}

async function boot(): Promise<void> {
  try {
    aois = await api.aois();
    const [{ OpsMap: OpsMapCls, legend }, ui] = await Promise.all([import("./map"), api.uiConfig()]);
    ops = new OpsMapCls($("map"), theme0, ui.basemap, (id) => void select(id, true));
    $("legend").replaceChildren(legend());
    await refresh();
  } catch (e) {
    $("queue").replaceChildren(h("div", { class: "empty" }, `API unavailable: ${String(e)}`));
  }
  live();
  setInterval(() => void refresh().catch(() => undefined), 60_000);
}
void boot();
