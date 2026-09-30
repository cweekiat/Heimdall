import "./style.css";
import { api } from "./api";
import type { Alert } from "./api";
import { ActionsBoard } from "./actionsboard";
import { renderBoard } from "./board";
import { ago, h } from "./dom";
import { view } from "./map3d";
import { icon, iconButton } from "./icons";
import { renderEntity, renderInspector } from "./inspector";
import { fmtLatLon, toMgrs } from "./geoutil";
import { explainBlock } from "./explain";
import { installKeys } from "./keys";
import { setMode } from "./theme/palette";
import { alertTitle, aoiName, CORROBORATION_LABEL, setAois, sourceLabel, T, truthLabel } from "./labels";
import type { OpsMap } from "./map";
import { showHelp, showShift } from "./overlays";
import { showAbout } from "./about";
import { maybeAutoStartTour } from "./tour/overlay";
import { PRIORITY, renderQueue, visibleAlerts } from "./panels";

/** Alerts drawn on the map: not expired or dismissed, unless the queue filter asks for a closed status. */
const OPEN_ON_MAP = ["new", "acknowledged", "under_review", "shelved", "confirmed"];
function mapAlerts(): ReturnType<typeof visibleAlerts> {
  const xs = visibleAlerts();
  const f = state.filters.status;
  return f && f !== "open" ? xs : xs.filter((a) => OPEN_ON_MAP.includes(a.status));
}
import { patch, refreshAll, refreshHealth, selectAlert, state, subscribe, triage, upsertAlert } from "./store";
import { renderTimeline } from "./timeline";
import { mountToolbar, setAlertBadge } from "./toolbar";
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
import { closeWindow, createWindow, EDGE_LEFT, EDGE_TOP, getBody, hasSavedGeometry, isOpen, openWindow, placeWindow, toggleWindow, winRect } from "./wm";

const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;

// ---- theme: dark -> light -> high contrast
const THEMES = ["dark", "light", "hc"] as const;
type Theme = (typeof THEMES)[number];
const stored = localStorage.getItem("theme") as Theme | null;
let theme: Theme = stored && THEMES.includes(stored) ? stored : matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
document.documentElement.dataset.theme = theme;
setMode(theme === "light" ? "light" : "dark");
let ops: OpsMap | null = null;
function cycleTheme(): void {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]!;
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("theme", theme);
  ops?.setTheme(theme === "light" ? "light" : "dark");
}

// ---- windows (the map is the whole viewport; everything else floats above it)
const layersBody = h("div", { id: "layers-body" });
createWindow({ id: "queue", title: T.alerts, icon: "alerts", body: $("queue"), x: EDGE_LEFT + 8, y: EDGE_TOP + 8, w: 380, h: 560 });
createWindow({ id: "board", title: T.areas, icon: "areas", body: $("board"), x: EDGE_LEFT + 8, y: EDGE_TOP + 8, w: 340, h: 340 });
createWindow({ id: "layers", title: T.layers, icon: "layers", body: layersBody, x: -60, y: EDGE_TOP + 8, w: 320, h: 520 });
const sourcesWin = new Sources();
const actionsWin = new ActionsBoard();
const areasWin = new Areas(() => state.aois);
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
  x: () => toggleWindow("actions"),
  o: () => toggleWindow("draw"),
  t: () => toggleWindow("timeline"),
  w: () => toggleWindow("watch"),
  h: shiftTool("shift", () => showShift()),
  "?": shiftTool("help", showHelp),
  i: shiftTool("about", showAbout),
};
const tools: Tool[] = [
  { id: "watch", icon: "activity", label: "Spectrum watch", key: "w", toggle: winToggles.w! },
  { id: "queue", icon: "alerts", label: T.alerts, key: "q", toggle: winToggles.q! },
  { id: "board", icon: "areas", label: T.areas, key: "b", toggle: winToggles.b! },
  { id: "layers", icon: "layers", label: T.layers, key: "l", toggle: winToggles.l! },
  { id: "sources", icon: "sources", label: T.sources, key: "u", toggle: winToggles.u! },
  { id: "draw", icon: "draw", label: T.drawAreas, key: "o", toggle: winToggles.o! },
  { id: "actions", icon: "actions", label: T.actions, key: "x", toggle: winToggles.x! },
  { id: "timeline", icon: "timeline", label: T.timeline, key: "t", toggle: winToggles.t! },
  { id: "shift", icon: "shift", label: T.shift, key: "h", toggle: winToggles.h! },
  { id: "help", icon: "help", label: T.help, key: "?", toggle: winToggles["?"]! },
  { id: "about", icon: "about", label: T.about, key: "i", toggle: winToggles.i! },
];
mountToolbar($("toolbar"), tools);
installKeys(winToggles);
$("brand").title = T.brandTip;

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
$("sb-tools").replaceChildren(
  swChip,
  iconButton("search", T.search, () => {
    openWindow("queue");
    $("q")?.focus();
  }, { cls: "sb-btn" }),
  iconButton("theme", T.theme, cycleTheme, { cls: "sb-btn" }),
);

// ---- map controls: small square icon buttons, top-right
const slider = h("input", { type: "range", min: "0", max: "24", step: "1", value: "0", "aria-label": "Look back (hours before now)" });
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
const toolBtn = (name: "measure" | "ring", label: string, t: "measure" | "ring"): HTMLButtonElement => {
  const b = iconButton(name, label, () => setTool(ops?.state.tool === t ? "none" : t), { pressed: false });
  b.dataset.tool = t;
  return b;
};
function setTool(t: "none" | "measure" | "ring"): void {
  ops?.setTool(t);
  document.querySelectorAll<HTMLButtonElement>("#mapctl [data-tool]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tool === t)));
}
const view3dBtn = iconButton(
  "cube",
  T.view3d,
  () => {
    const next = ops?.viewMode() === "3d" ? "2d" : "3d";
    ops?.setView(next);
    view3dBtn.setAttribute("aria-pressed", String(next === "3d"));
  },
  { pressed: false },
);
$("mapctl").replaceChildren(
  iconButton("zoomIn", T.zoomIn, () => ops?.zoomBy(1)),
  iconButton("zoomOut", T.zoomOut, () => ops?.zoomBy(-1)),
  toolBtn("measure", T.measure, "measure"),
  toolBtn("ring", T.ring, "ring"),
  iconButton("layers", `${T.layers} (l)`, winToggles.l!),
  view3dBtn,
  iconButton("compass", T.compass, () => ops?.resetNorth()),
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
        h("div", {}, h("strong", {}, `${PRIORITY[a.severity]!.icon} ${alertTitle(a)}`), explainBlock(a), h("div", {}, `${sourceLabel(a.admiralty.reliability)} · ${truthLabel(a.admiralty.credibility)} · ${CORROBORATION_LABEL[a.corroboration_status] ?? a.corroboration_status} · ${ago(a.last_seen)}`)),
      ),
      ...(as.length > 4 ? [h("div", { class: "note" }, `+${as.length - 4} more (click to zoom)`)] : []),
    );
  };
  update();
}

// ---- status strip
function renderClock(): void {
  $("clock").textContent = new Date().toISOString().slice(0, 19).replace("T", " ") + "Z";
}
setInterval(renderClock, 1000);
renderClock();

const isOpenA = (a: Alert): boolean => ["new", "acknowledged", "under_review"].includes(a.status);
const SEV_ORDER = ["critical", "high", "medium", "low", "info"] as const;

function renderStatus(): void {
  const open = state.alerts.filter(isOpenA);
  const counts: Record<string, number> = { P1: 0, P2: 0, P3: 0, P4: 0 };
  open.forEach((a) => (counts[PRIORITY[a.severity]!.p]! += 1));
  const now = Date.now();
  const lastH = state.alerts.filter((a) => now - new Date(a.first_seen).getTime() < 3600_000).length;
  const avg = state.alerts.filter((a) => now - new Date(a.first_seen).getTime() < 24 * 3600_000).length / 24;
  const flood = lastH > Math.max(3, avg * 2);
  const sevOf = { P1: "critical", P2: "high", P3: "medium", P4: "low" } as const;
  $("sb-counts").replaceChildren(
    ...(Object.entries(counts) as [keyof typeof sevOf, number][]).map(([p, n]) => {
      const icoText = p === "P1" ? "▲▲" : p === "P2" ? "▲" : p === "P3" ? "■" : "●";
      const word = { P1: "Critical", P2: "High", P3: "Medium", P4: "Low" }[p];
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

// Critical toast: floating, persistent, never auto-jumps the view.
function renderToast(): void {
  const b = $("toast");
  const crit = state.alerts.filter((a) => a.severity === "critical" && a.status === "new").sort((x, y) => y.last_seen.localeCompare(x.last_seen));
  const a = crit[0];
  if (!a) {
    b.hidden = true;
    return;
  }
  b.hidden = false;
  b.replaceChildren(
    icon("warn"),
    h("strong", {}, alertTitle(a)),
    h("span", { class: "note" }, `${aoiName(a.aoi)} · ${ago(a.first_seen)}${crit.length > 1 ? ` · +${crit.length - 1} more` : ""}`),
    h("button", { class: "primary", onclick: () => void selectAlert(a.id) }, T.toastGoTo),
    h("button", { onclick: () => void triage(a.id, "acknowledged") }, T.toastAck),
  );
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
  // first run: open the alert list so the analyst lands on something useful (wide screens only)
  if (!localStorage.getItem("hm.wm.v1") && window.innerWidth >= 1100) openWindow("queue");
  try {
    const aois = await api.aois();
    setAois(aois);
    patch({ aois });
    setTimeout(maybeAutoStartTour, 600);
    const [{ OpsMap: OpsMapCls, legend }, ui] = await Promise.all([import("./map"), api.uiConfig()]);
    ops = new OpsMapCls($("map"), theme === "light" ? "light" : "dark", ui.basemap, ui.overlays, (id) => void selectAlert(id, false));
    $("legend").replaceChildren(legend());
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
      void api.aois().then((next) => {
        setAois(next);
        patch({ aois: next });
        areasWin.onChange();
      });
      void refreshAll().catch(() => undefined);
    };
    void areasWin.reload();
    wireMapExtras(ops);
    const traffic = new Traffic(ops, $("mapwrap"));
    const notices = new Notices(ops);
    const p3 = new P3Layers(ops, $("mapwrap"));
    p3.aois = () => state.aois.map((a) => a.name);
    ctxHooks.onFootprint = (id) => p3.focus(id, () => setFilter({ p3: [...filters.p3, "imagery"] }));
    const spec = new SpectrumLayers(ops, $("mapwrap")); // task 030: band ladder + spectrum layers
    ops.extraLayers = (tool, zoom) => [...spec.layers(tool), ...p3.layers(tool), ...notices.layers(tool), ...traffic.layers(tool, zoom)];
    const filterBox = h("div", { class: "filters" });
    const refreshFilters = mountFilters(filterBox, {
      counts: traffic.getCounts,
      sources: () => sourcesWin.list,
      extra: () => p3.chipList(),
      live: () => spec.live(),
      daily: () => spec.daily(),
    });
    traffic.onCounts = refreshFilters;
    sourcesWin.onChange = () => {
      traffic.setSources(sourcesWin.list);
      spec.update(state.alerts, sourcesWin.list);
      refreshFilters();
    };
    subscribe(() => spec.update(state.alerts, sourcesWin.list));
    // 033: spectrum watch board (Monitor -> Detect -> Investigate)
    const watch = new SpectrumWatch({
      flyTo: (name) => {
        const a = state.aois.find((x) => x.name === name);
        if (a) ops?.flyTo(a.lon, a.lat);
      },
      openAlert: (id) => void selectAlert(id),
      alerts: () => state.alerts,
    });
    watch.onStatus = (s) => spec.setStatus(s);
    if (!hasSavedGeometry("watch")) openWindow("watch");
    let lastBand = filters.band;
    onFilters(() => {
      if (ops) ops.state.alerts = filters.alerts ? mapAlerts() : [];
      ops?.render();
      if (filters.band !== lastBand) {
        lastBand = filters.band;
        patch({ filters: { ...state.filters } }); // redraw the alert queue with the band filter
      }
    });
    getBody("layers")!.replaceChildren(
      filterBox,
      h("fieldset", {}, h("legend", {}, T.mapExtras), h("label", { for: "cov", class: "chk" }, cov, T.coverageLayer), h("label", { class: "chk" }, "Look back ", slider, sliderLbl)),
      h(
        "fieldset",
        {},
        h("legend", {}, "3D view"),
        h("label", { class: "chk" }, "Height exaggeration ", h("input", { type: "range", min: "1", max: "10", value: String(view.exaggeration), oninput: (e: Event) => { view.exaggeration = Number((e.target as HTMLInputElement).value); ops?.render(); } })),
        h("label", { class: "chk" }, h("input", { type: "checkbox", onchange: (e: Event) => { view.drop = (e.target as HTMLInputElement).checked; ops?.render(); } }), "Lines from aircraft down to the ground (3D only)"),
      ),
      ops.layers.panel(),
    );
    void sourcesWin.refresh().then(() => {
      traffic.start();
      void notices.refresh();
      p3.start();
      spec.start();
      watch.start();
    });
    void refreshSpaceWeather();
    setInterval(() => void refreshSpaceWeather(), 300_000);
    setInterval(() => void notices.refresh(), 300_000);
    setInterval(() => void sourcesWin.refresh(), 60_000);
    void actionsWin.refresh();
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
