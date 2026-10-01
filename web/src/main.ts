import "./style.css";
import { fmtClock, onTz } from "./tz";
import { api } from "./api";
import type { Alert } from "./api";
import { renderBoard } from "./board";
import { ago, h } from "./dom";
import { icon, iconButton } from "./icons";
import { renderEntity, renderInspector } from "./inspector";
import { fmtLatLon, toMgrs } from "./geoutil";
import { explainBlock, sourceNames } from "./explain";
import { installKeys } from "./keys";
import { setMode } from "./theme/palette";
import { Loading } from "./loading";
import { mountLegend, mountMapType } from "./maptype";
import { mountSearch, parseLatLon } from "./search";
import type { Hit } from "./search";
import { SettingsPage, THEMES } from "./settings";
import type { Theme } from "./settings";
import { Satellites } from "./satellites";
import { alertTitle, aoiName, kindLabel, setAois, T } from "./labels";
import type { OpsMap } from "./map";
import { showHelp, showShift } from "./overlays";
import { showAbout } from "./about";
import { maybeAutoStartTour, tourMap } from "./tour/overlay";
import { demoLayers, tourDemo } from "./tour/demo";
import { isCritical, PRIORITY, renderQueue, visibleAlerts } from "./panels";

/** Alerts drawn on the map: not expired or dismissed, unless the queue filter asks for a closed status. */
const OPEN_ON_MAP = ["new", "acknowledged", "under_review", "shelved", "confirmed"];
function mapAlerts(): ReturnType<typeof visibleAlerts> {
  const xs = visibleAlerts().filter((a) => !filters.hiddenKinds.includes(a.kind)); // Detections filter
  const f = state.filters.status;
  return f && f !== "open" ? xs : xs.filter((a) => OPEN_ON_MAP.includes(a.status));
}
import { patch, refreshAll, refreshHealth, selectAlert, state, subscribe, triage, upsertAlert } from "./store";
import { renderTimeline } from "./timeline";
import { mountToolbar } from "./toolbar";
import type { Tool } from "./toolbar";
import { Sources } from "./sources";
import { Areas } from "./areas";
import { Traffic } from "./traffic";
import { Notices } from "./notices";
import { P3Layers } from "./p3layers";
import { SpectrumLayers } from "./spectrum";
import { SpectrumWatch } from "./watch";
import { filters, mountFilters, onFilters, setFilter } from "./filters";
import { ctxHooks } from "./context";
import { closeWindow, createWindow, EDGE_LEFT, EDGE_TOP, getBody, hasSavedGeometry, isOpen, onWmChange, openWindow, placeWindow, toggleWindow, winRect } from "./wm";

const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;

const loading = new Loading();
const doneMap = loading.step("map", "Loading the map");
window.setTimeout(doneMap, 8000); // the basemap keeps loading behind; never hold the page longer
const doneAreas = loading.step("areas", "Loading areas of operation");
const doneAlerts = loading.step("alerts", "Loading alerts and data sources");

// ---- theme: dark, light or high contrast (Settings page)
const stored = localStorage.getItem("theme") as Theme | null;
let theme: Theme = stored && THEMES.includes(stored) ? stored : matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
document.documentElement.dataset.theme = theme;
setMode(theme === "light" ? "light" : "dark");
let ops: OpsMap | null = null;
function setTheme(t: Theme): void {
  theme = t;
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("theme", theme);
  } catch {
    /* private mode */
  }
  ops?.setTheme(theme === "light" ? "light" : "dark");
}

// ---- windows (the map is the whole viewport; everything else floats above it)
const layersBody = h("div", { id: "layers-body" });
createWindow({ id: "queue", title: T.alerts, icon: "alerts", body: $("queue"), x: -12, y: EDGE_TOP + 8, w: 400, h: 600 });
createWindow({ id: "board", title: T.areas, icon: "areas", body: $("board"), x: EDGE_LEFT + 8, y: EDGE_TOP + 8, w: 340, h: 340 });
createWindow({ id: "layers", title: T.layers, icon: "filter", body: layersBody, x: EDGE_LEFT + 8, y: EDGE_TOP + 8, w: 360, h: 700 });
const sourcesWin = new Sources();
const areasWin = new Areas(() => state.aois);
areasWin.busyHook = (label, p) => loading.busy(label, p);
areasWin.openWatch = () => openWindow("watch");
areasWin.openBoard = () => openWindow("board");
const settingsWin = new SettingsPage({ theme: () => theme, setTheme, mapPanel: () => ops?.layers.panel() ?? null });
createWindow({ id: "timeline", title: T.timeline, icon: "timeline", body: $("timeline"), x: EDGE_LEFT + 8, y: -12, w: 820, h: 220, minH: 140 });
createWindow({
  id: "inspector",
  title: "Alert details",
  icon: "alerts",
  body: $("inspector"),
  x: -60,
  y: EDGE_TOP + 8,
  w: 440,
  h: 640,
  minW: 320,
  onClose: () => {
    if (state.selected !== null) patch({ selected: null, detail: null, tracks: [], selectedEntity: null });
  },
});

const shiftTool = (id: string, fn: () => void): (() => void) => () => (isOpen(id) ? closeWindow(id) : fn());
const winToggles: Record<string, () => void> = {
  q: () => toggleWindow("queue"),
  b: () => toggleWindow("board"),
  l: () => toggleWindow("layers"),
  u: () => toggleWindow("sources"),
  o: () => toggleWindow("draw"),
  t: () => toggleWindow("timeline"),
  w: () => toggleWindow("watch"),
  ",": () => toggleWindow("settings"),
  g: () => ops?.toggleGlobe(),
  "0": () => ops?.home(),
  h: shiftTool("shift", () => showShift()),
  "?": shiftTool("help", showHelp),
  i: shiftTool("about", showAbout),
};
// Left rail: pages only. Alerts, timeline and shift report live top right; filters by the search box.
const tools: Tool[] = [
  { id: "draw", icon: "draw", label: T.drawAreas, key: "o", toggle: winToggles.o!, hint: "Draw, list and monitor areas" },
  { id: "sources", icon: "sources", label: T.sources, key: "u", toggle: winToggles.u!, hint: "Source health, on/off, API keys" },
  { id: "settings", icon: "settings", label: "Settings", key: ",", toggle: winToggles[","]!, hint: "Theme, map overlays, layout" },
];
const railBottom: Tool[] = [
  { id: "help", icon: "help", label: T.help, key: "?", toggle: winToggles["?"]!, hint: "Shortcuts, map key, guided tour" },
  { id: "about", icon: "about", label: T.about, key: "i", toggle: winToggles.i!, hint: "What Heimdall is and how it was built" },
];
mountToolbar($("toolbar"), tools, railBottom);
installKeys(winToggles);
$("brand").title = T.brandTip;

// ---- top right: alerts (with badge), timeline, shift report
const alertBadge = h("span", { class: "tb-badge", hidden: true, "aria-live": "polite" });
const topBtn = (id: string, ic: Parameters<typeof icon>[0], label: string, key: string, fn: () => void, extra?: HTMLElement): HTMLButtonElement => {
  const b = h("button", { type: "button", class: "top-btn", "aria-pressed": "false", title: `${label} (${key})`, "aria-label": label, onclick: fn }, icon(ic, 18), h("span", { class: "top-label" }, label), extra ?? null) as HTMLButtonElement;
  b.dataset.for = id;
  return b;
};
const alertsBtn = topBtn("queue", "alerts", T.alerts, "q", winToggles.q!, alertBadge);
$("sb-actions").replaceChildren(
  alertsBtn,
  topBtn("timeline", "timeline", T.timeline, "t", winToggles.t!),
  topBtn("shift", "shift", "Shift report", "h", winToggles.h!),
);
function setAlertBadge(count: number, worst: string): void {
  alertBadge.hidden = count === 0;
  alertBadge.textContent = count > 99 ? "99+" : String(count);
  alertBadge.className = `tb-badge sev-${worst}`;
  alertsBtn.setAttribute("aria-label", `${T.alerts}: ${count} unacknowledged`);
}
onWmChange(() => {
  document.querySelectorAll<HTMLButtonElement>("#sb-actions [data-for]").forEach((b) => b.setAttribute("aria-pressed", String(isOpen(b.dataset.for!))));
  filterBtn.setAttribute("aria-pressed", String(isOpen("layers")));
});

// ---- top-right status tools: search + theme
const swChip = h("span", { class: "sw-chip note", title: "Space weather (NOAA SWPC): storms and flares can disturb HF and GPS on their own" }, "Space weather …");
async function refreshSpaceWeather(): Promise<void> {
  try {
    const w = await api.spaceWeather();
    if (!w.available) {
      swChip.textContent = "Space weather: no data";
      return;
    }
    const kp = typeof w.kp === "number" ? w.kp : null;
    swChip.textContent = `Kp ${kp ?? "?"} · G${w.g_scale ?? 0} R${w.r_scale ?? 0} S${w.s_scale ?? 0}`;
    swChip.classList.toggle("sw-storm", (kp ?? 0) >= 5 || (w.r_scale ?? 0) >= 1);
  } catch {
    swChip.textContent = "Space weather: unavailable";
  }
}
$("sb-tools").replaceChildren(swChip);

// ---- Filters: a prominent button beside the map type (bottom left)
const filterBtn = h("button", { type: "button", class: "filter-btn", "aria-pressed": "false", title: "Filters: show or hide data on the map (l)", onclick: () => winToggles.l!() }, icon("filter", 18), h("span", {}, "Filters"));

// ---- centre search: coordinates, areas, alerts, loaded traffic
let trafficSearch: (q: string) => Hit[] = () => [];
function findAll(q: string): Hit[] {
  const out: Hit[] = [];
  const ll = parseLatLon(q);
  if (ll) out.push({ group: "Go to", label: fmtLatLon(ll[0], ll[1]), detail: `MGRS ${toMgrs(ll[0], ll[1])}`, pick: () => ops?.flyTo(ll[1], ll[0]) });
  const s = q.trim().toLowerCase();
  for (const a of state.aois) {
    const label = a.label ?? aoiName(a.name);
    if (label.toLowerCase().includes(s) || a.name.toLowerCase().includes(s))
      out.push({ group: "Areas of operation", label, detail: `${Math.round(a.radius_nm)} nm`, pick: () => ops?.flyTo(a.lon, a.lat) });
  }
  let n = 0;
  for (const a of state.alerts) {
    if (n >= 6) break;
    const hay = `${a.ref ?? ""} ${alertTitle(a)} ${a.title} ${a.kind}`.toLowerCase();
    if (!hay.includes(s)) continue;
    n += 1;
    out.push({ group: "Alerts", label: alertTitle(a), detail: `${a.ref ?? ""} · ${ago(a.last_seen)}`.replace(/^ · /, ""), pick: () => void selectAlert(a.id) });
  }
  out.push(...trafficSearch(q));
  if (s.length >= 2) out.push({ group: "Traffic filter", label: `Show only traffic matching "${q.trim()}"`, detail: "Sets the quick filter (clear it in Filters)", pick: () => setFilter({ search: q.trim() }) });
  return out;
}
mountSearch($("search"), { find: findAll });

// ---- map controls, bottom right (Google Maps style): tools, recenter, compass, globe, zoom
const slider = h("input", { type: "range", min: "0", max: "24", step: "1", value: "0", "aria-label": "Look back (hours before now)" });
const sliderLbl = h("span", {}, "now");
slider.addEventListener("input", () => {
  if (ops) ops.state.hoursAgo = Number(slider.value);
  sliderLbl.textContent = slider.value === "0" ? "now" : `-${slider.value} h`;
  ops?.render();
});
const lookBack = h("label", { class: "chk" }, "Look back ", slider, sliderLbl);
const toolBtn = (name: "measure" | "ring", label: string, t: "measure" | "ring"): HTMLButtonElement => {
  const b = iconButton(name, label, () => setTool(ops?.state.tool === t ? "none" : t), { pressed: false });
  b.dataset.tool = t;
  return b;
};
function setTool(t: "none" | "measure" | "ring"): void {
  ops?.setTool(t);
  document.querySelectorAll<HTMLButtonElement>("#mapctl [data-tool]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tool === t)));
}
const globeBtn = h("button", { type: "button", class: "ctl-btn globe-btn", title: `${T.view3d} (g)`, "aria-label": T.view3d, onclick: () => ops?.toggleGlobe() }, "3D") as HTMLButtonElement;
const showProjection = (globe: boolean): void => {
  globeBtn.textContent = globe ? "2D" : "3D";
  globeBtn.title = globe ? "Flat 2D map (g)" : "3D globe (g)";
  globeBtn.setAttribute("aria-label", globe ? "Switch to the flat 2D map" : "Switch to the 3D globe");
};
$("mapctl").replaceChildren(
  h("div", { class: "ctl-group" }, toolBtn("measure", T.measure, "measure"), toolBtn("ring", T.ring, "ring")),
  h("div", { class: "ctl-group" }, iconButton("home", "Recenter on South-East Asia (0)", () => ops?.home()), iconButton("compass", T.compass, () => ops?.resetNorth())),
  globeBtn,
  h("div", { class: "ctl-group zoom" }, iconButton("zoomIn", T.zoomIn, () => ops?.zoomBy(1)), iconButton("zoomOut", T.zoomOut, () => ops?.zoomBy(-1))),
);
const hover = h("div", { class: "hovercard", hidden: true });
$("mapwrap").append(hover);
function wireMapExtras(m: OpsMap): void {
  const ro = $("readout");
  const update = (lon?: number, lat?: number): void => {
    const tool = m.toolReadout();
    const pos = lon === undefined || lat === undefined ? "" : `${fmtLatLon(lat, lon)} · MGRS ${toMgrs(lat, lon)}`;
    ro.replaceChildren(h("div", {}, pos || "Move cursor over the map"), ...(tool ? [h("div", {}, tool)] : []));
  };
  m.onCursor = (lon, lat) => update(lon, lat);
  m.onTool = () => update();
  m.onHover = (as, x, y) => {
    if (!as) {
      hover.hidden = true;
      return;
    }
    hover.hidden = false;
    hover.style.left = `${x + 12}px`;
    hover.style.top = `${y + 12}px`;
    hover.replaceChildren(
      ...as.slice(0, 4).map((a) =>
        h("div", {}, h("strong", {}, `${PRIORITY[a.severity]!.icon} ${alertTitle(a)}`), explainBlock(a), h("div", { class: "note" }, `last seen ${ago(a.last_seen)}`)),
      ),
      ...(as.length > 4 ? [h("div", { class: "note" }, `+${as.length - 4} more (click to zoom)`)] : []),
    );
  };
  update();
}

// ---- status strip
function renderClock(): void {
  $("clock").textContent = fmtClock();
}
setInterval(renderClock, 1000);
renderClock();
// display time zone changed (Settings): redraw everything that shows a time
onTz(() => {
  renderClock();
  patch({ filters: { ...state.filters } });
  if (state.detail) renderInspector($("inspector"));
  renderToast();
});

const isOpenA = (a: Alert): boolean => ["new", "acknowledged", "under_review"].includes(a.status);
const SEV_ORDER = ["critical", "high", "medium", "low", "info"] as const;

function renderStatus(): void {
  const open = state.alerts.filter(isOpenA);
  const counts: Record<string, number> = { P1: 0, P2: 0 };
  open.forEach((a) => (counts[PRIORITY[a.severity]!.p]! += 1));
  const now = Date.now();
  const lastH = state.alerts.filter((a) => now - new Date(a.first_seen).getTime() < 3600_000).length;
  const avg = state.alerts.filter((a) => now - new Date(a.first_seen).getTime() < 24 * 3600_000).length / 24;
  const flood = lastH > Math.max(3, avg * 2);
  const sevOf = { P1: "critical", P2: "medium" } as const;
  $("sb-counts").replaceChildren(
    ...(Object.entries(counts) as [keyof typeof sevOf, number][]).map(([p, n]) => {
      const icoText = p === "P1" ? "▲" : "●";
      const word = { P1: "Critical", P2: "Warning" }[p];
      return h(
        "button",
        {
          class: `pill sev-${sevOf[p]}`,
          "aria-pressed": String(state.filters.priority === p),
          "aria-label": `${word} priority: ${n} open`,
          title: `${word} priority: ${n} open${flood ? ` (busy: ${lastH} new in the last hour)` : ""}`,
          onclick: () => {
            patch({ filters: { ...state.filters, priority: state.filters.priority === p ? "" : p, status: state.filters.priority === p ? state.filters.status : "open" } });
            openWindow("queue");
          },
        },
        `${icoText} ${n}`,
      );
    }),
  );
  const unack = state.alerts.filter((a) => a.status === "new");
  const worst = SEV_ORDER.find((s) => unack.some((a) => a.severity === s)) ?? "info";
  setAlertBadge(unack.length, worst);
  // source health: one dot summarising the worst state; click opens Data sources
  const rank = { ok: 0, warn: 1, bad: 2 } as const;
  let worstG: "ok" | "warn" | "bad" = "ok";
  let bad = 0;
  for (const s of state.health) {
    const age = s.last_ok ? (Date.now() - new Date(s.last_ok).getTime()) / 1000 : Infinity;
    const g = age < s.expected_latency_s * 2 ? "ok" : age < s.expected_latency_s * 5 ? "warn" : "bad";
    if (g !== "ok") bad += 1;
    if (rank[g] > rank[worstG]) worstG = g;
  }
  const word = worstG === "ok" ? "All sources up to date" : worstG === "warn" ? `${bad} source(s) slow` : `${bad} source(s) down or stale`;
  $("sb-fresh").replaceChildren(
    h("button", { class: "pill", title: `${word} (open Data sources)`, "aria-label": word, onclick: winToggles.u! }, h("span", { class: `dot ${worstG}` }), icon("sources", 14)),
  );
}

// Critical alerts: a small card pinned above each red dot (open alerts only; closable per alert).
const popLayer = h("div", { class: "crit-pops", "aria-label": "Critical alerts on the map" });
$("mapwrap").append(popLayer);
const closedPops = new Set<string>();
const MAX_POPS = 6;
let pops: { a: Alert; el: HTMLElement }[] = [];
function renderToast(): void {
  $("toast").hidden = true; // replaced by the pinned cards
  const crit = state.alerts
    .filter((a) => isCritical(a) && OPEN_ON_MAP.slice(0, 3).includes(a.status) && a.lat !== null && a.lon !== null && !closedPops.has(a.id))
    .filter((a) => filters.alerts && !filters.hiddenKinds.includes(a.kind))
    .sort((x, y) => y.last_seen.localeCompare(x.last_seen))
    .slice(0, MAX_POPS);
  pops = crit.map((a) => {
    const el = h(
      "div",
      { class: "crit-pop", role: "alert" },
      h(
        "div",
        { class: "crit-head" },
        h("span", { class: "crit-tag" }, "▲ CRITICAL"),
        h("button", { type: "button", class: "crit-x", "aria-label": "Hide this card", title: "Hide this card", onclick: () => { closedPops.add(a.id); renderToast(); } }, "×"),
      ),
      h("strong", {}, kindLabel(a.kind)),
      h("div", { class: "note" }, `${aoiName(a.aoi)} · ${ago(a.first_seen)}${a.ref ? ` · ${a.ref}` : ""}`),
      h("div", { class: "crit-sum" }, `Sources: ${sourceNames(a)} · confidence ${Math.round(a.confidence * 100)} %`),
      h(
        "div",
        { class: "row" },
        h("button", { type: "button", class: "primary", onclick: () => void selectAlert(a.id, false) }, "Details"),
        h("button", { type: "button", onclick: () => void triage(a.id, "acknowledged") }, T.ack),
      ),
    );
    return { a, el };
  });
  popLayer.replaceChildren(...pops.map((p) => p.el));
  placePops();
}
function placePops(): void {
  if (!ops) return;
  const r = $("map").getBoundingClientRect();
  // whole-region / globe view: the red dots are enough; tour example scenes keep the map clear
  const far = ops.zoom() < 3 || tourDemo.scene !== null;
  const placed: { x: number; y: number; w: number; h: number }[] = [];
  for (const { a, el } of pops) {
    const p = ops.project(a.lon as number, a.lat as number);
    let vis = !far && ops.facing(a.lon as number, a.lat as number) && p.x > 0 && p.y > 0 && p.x < r.width && p.y < r.height;
    if (vis) {
      // newest first: a card that would overlap an already placed one stays hidden
      const w = 250;
      const hh = el.offsetHeight || 120;
      const below = p.y - hh - 16 < 64;
      const box = { x: p.x - w / 2, y: below ? p.y + 16 : p.y - hh - 16, w, h: hh };
      if (placed.some((b) => box.x < b.x + b.w && b.x < box.x + box.w && box.y < b.y + b.h && b.y < box.y + box.h)) vis = false;
      else placed.push(box);
    }
    el.hidden = !vis;
    if (!vis) continue;
    // above the dot when there is room under the top bar, else below it
    const below = p.y - el.offsetHeight - 16 < 64;
    el.classList.toggle("below", below);
    el.style.transform = `translate(${Math.round(p.x)}px, ${Math.round(p.y)}px) translate(-50%, ${below ? "16px" : "calc(-100% - 16px)"})`;
  }
}

// ---- linked rendering
let lastSelected: string | null = null;
/** Open the details window beside the selected alert (its map marker, else the alert list); once the analyst moves it, that spot is remembered. */
function placeInspector(): void {
  if (hasSavedGeometry("inspector") || window.innerWidth < 768) return;
  const a = state.alerts.find((x) => x.id === state.selected);
  const w = 440;
  const vw = window.innerWidth;
  if (ops && a && a.lat !== null && a.lon !== null) {
    const p = ops.project(a.lon, a.lat);
    const off = $("map").getBoundingClientRect();
    const px = p.x + off.left;
    const py = p.y + off.top;
    if (px >= 0 && px <= vw && py >= 0) {
      placeWindow("inspector", px + 26 + w <= vw - 8 ? px + 26 : px - 26 - w, py - 60);
      return;
    }
  }
  const q = winRect("queue");
  if (q) placeWindow("inspector", q.x + q.w + 8 + w <= vw ? q.x + q.w + 8 : q.x - w - 8, q.y);
}
function draw(changed: Set<string>): void {
  const has = (...k: string[]): boolean => k.some((x) => changed.has(x));
  if (has("alerts", "filters", "selected", "checked", "window")) {
    if (!(document.activeElement instanceof HTMLInputElement && document.activeElement.id === "q")) renderQueue($("queue"));
    else document.getElementById("qcount") && (document.getElementById("qcount")!.textContent = `${T.alerts} (${visibleAlerts().length})`);
  }
  if (has("summary", "selectedAoi")) renderBoard($("board"));
  if (has("alerts", "summary", "health", "filters", "selectedAoi")) {
    renderStatus();
    renderToast();
  }
  if (has("detail", "tab", "selected", "selectedEntity", "tracks")) {
    if (state.selectedEntity) renderEntity($("inspector"), state.selectedEntity);
    else renderInspector($("inspector"));
  }
  if (has("selected") && state.selected !== lastSelected) {
    lastSelected = state.selected;
    if (state.selected) {
      openWindow("inspector");
      placeInspector();
    }
    else closeWindow("inspector");
  }
  if (has("alerts", "window", "playing", "filters")) renderTimeline($("timeline"));
  if (ops && has("alerts", "cells", "aois", "selected", "detail", "tracks", "filters", "window")) {
    Object.assign(ops.state, {
      alerts: filters.alerts ? mapAlerts() : [],
      cells: state.cells,
      aois: state.aois,
      aoAreas: areasWin.display(),
      selected: state.selected,
      evidence: state.detail?.evidence ?? [],
      tracks: state.tracks,
      asOf: state.window?.to ?? null,
      showCoverage: filters.coverage,
    });
    ops.render();
  }
  if (ops && has("fly") && state.fly) ops.flyTo(state.fly.lon, state.fly.lat);
}
subscribe(draw);

// ---- live updates (SSE) with polling fallback
function live(): void {
  const es = new EventSource("/api/stream");
  const dot = $("live");
  const set = (on: boolean, word: string): void => {
    dot.classList.toggle("on", on);
    dot.title = word;
    dot.setAttribute("aria-label", word);
  };
  es.onopen = () => set(true, T.live);
  es.addEventListener("snapshot", (e) => patch({ alerts: JSON.parse((e as MessageEvent<string>).data) as Alert[] }));
  es.addEventListener("alert", (e) => upsertAlert(JSON.parse((e as MessageEvent<string>).data) as Alert));
  es.onerror = () => set(false, T.reconnecting);
}

async function boot(): Promise<void> {
  try {
    const aois = await api.aois();
    setAois(aois);
    patch({ aois });
    doneAreas();
    setTimeout(maybeAutoStartTour, 600);
    const [{ OpsMap: OpsMapCls, legend }, ui] = await Promise.all([import("./map"), api.uiConfig()]);
    ops = new OpsMapCls($("map"), theme === "light" ? "light" : "dark", ui.basemap, ui.overlays, (id) => void selectAlert(id, false));
    ops.onReady = doneMap;
    ops.onViewChange = placePops;
    ops.onProjection = showProjection;
    mountLegend($("legend"), legend());
    mountMapType($("maptype"), ops.layers, () => openWindow("settings"), [filterBtn, $("legend")]);
    if (isOpen("settings")) settingsWin.render();
    if (ui.initial_view) ops.jumpTo(ui.initial_view.lon, ui.initial_view.lat, ui.initial_view.zoom);
    if (ui.banner) {
      const b = document.createElement("div");
      b.className = "replay-banner";
      b.setAttribute("role", "status");
      b.textContent = ui.banner; // server text, rendered as text only
      document.body.append(b);
    }
    areasWin.attach(ops);
    areasWin.onChange = () => {
      if (!ops) return;
      ops.state.aoAreas = areasWin.display();
      ops.render();
    };
    areasWin.onServerChange = () => {
      // the new outline is already drawn (onChange); refresh the rest without blocking the page
      void api.aois().then((next) => {
        setAois(next);
        patch({ aois: next });
        areasWin.onChange();
      });
      window.setTimeout(() => void refreshAll().catch(() => undefined), 0);
    };
    void areasWin.reload();
    wireMapExtras(ops);
    const traffic = new Traffic(ops, $("mapwrap"));
    trafficSearch = (q) => traffic.search(q).map((t) => ({ group: "Aircraft and ships in view", label: t.label, detail: t.detail, pick: () => (ops?.flyTo(t.lon, t.lat), t.pick()) }));
    const notices = new Notices(ops);
    const p3 = new P3Layers(ops, $("mapwrap"));
    p3.aois = () => state.aois.map((a) => a.name);
    ctxHooks.onFootprint = (id) => p3.focus(id, () => setFilter({ p3: [...filters.p3, "imagery"] }));
    const spec = new SpectrumLayers(ops, $("mapwrap")); // task 030: band ladder + spectrum layers
    const sats = new Satellites(ops, $("mapwrap"));
    ops.extraLayers = (tool, zoom) => [...spec.layers(tool), ...p3.layers(tool), ...notices.layers(tool), ...traffic.layers(tool, zoom), ...(sats.layers() as never[]), ...(demoLayers() as never[])];
    tourMap.view = (lon, lat, zoom) => ops?.view(lon, lat, zoom);
    tourMap.redraw = () => (ops?.render(), placePops());
    tourMap.home = () => ops?.home();
    const filterBox = h("div", { class: "filter-page" });
    const refreshFilters = mountFilters(
      filterBox,
      {
        counts: traffic.totals,
        satellites: sats.count,
        alertKinds: () => {
          const c: Record<string, number> = {};
          for (const a of state.alerts) if (OPEN_ON_MAP.slice(0, 3).includes(a.status)) c[a.kind] = (c[a.kind] ?? 0) + 1;
          return c;
        },
        spectrumExtra: (l) => spec.extra(l),
        spectrumStatus: (l) => spec.layerStatus(l),
        extra: () => p3.chipList(),
      },
      lookBack,
    );
    traffic.onCounts = refreshFilters;
    spec.onPanels = refreshFilters;
    sats.onLoad = refreshFilters;
    subscribe((ch) => ch.has("alerts") && refreshFilters());
    sourcesWin.onChange = () => {
      traffic.setSources(sourcesWin.list);
      spec.update(state.alerts, sourcesWin.list);
      refreshFilters();
    };
    subscribe(() => spec.update(state.alerts, sourcesWin.list));
    // 033: spectrum watch board (Monitor -> Detect -> Investigate), opened from Area of Ops
    const watch = new SpectrumWatch({
      flyTo: (name) => {
        const a = state.aois.find((x) => x.name === name);
        if (a) ops?.flyTo(a.lon, a.lat);
      },
      openAlert: (id) => void selectAlert(id),
      alerts: () => state.alerts,
    });
    watch.onStatus = (s) => spec.setStatus(s);
    let lastBand = filters.band;
    onFilters(() => {
      if (ops) {
        ops.state.alerts = filters.alerts ? mapAlerts() : [];
        ops.state.showCoverage = filters.coverage;
      }
      ops?.render();
      if (filters.band !== lastBand) {
        lastBand = filters.band;
        patch({ filters: { ...state.filters } }); // redraw the alert queue with the band filter
      }
    });
    getBody("layers")!.replaceChildren(filterBox);
    ops.state.showCoverage = filters.coverage;
    // each layer starts on its own: one failing must not stop the others
    const safe = (name: string, fn: () => unknown): void => {
      try {
        void fn();
      } catch (e) {
        console.error(`${name} failed to start`, e);
      }
    };
    void sourcesWin.refresh().finally(() => {
      safe("traffic", () => traffic.start());
      safe("notices", () => notices.refresh());
      safe("p3", () => p3.start());
      safe("spectrum", () => spec.start());
      safe("watch", () => watch.start());
      safe("satellites", () => sats.start());
    });
    void refreshSpaceWeather();
    setInterval(() => void refreshSpaceWeather(), 300_000);
    setInterval(() => void notices.refresh(), 300_000);
    setInterval(() => void sourcesWin.refresh(), 60_000);
    await Promise.all([refreshAll(), refreshHealth().catch(() => undefined)]);
    patch({ aois });
    doneAlerts();
  } catch (e) {
    console.error("boot failed", e);
    loading.finish();
    $("queue").replaceChildren(h("div", { class: "empty" }, `API unavailable: ${String(e)}`));
    openWindow("queue");
  }
  live();
  setInterval(() => void refreshAll().catch(() => undefined), 60_000);
  setInterval(() => void refreshHealth().catch(() => undefined), 60_000);
}
void boot();
