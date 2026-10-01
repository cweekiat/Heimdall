// Bottom-left map-type picker (Google Maps style): one tile shows the map type you would switch to;
// hovering or focusing it opens a row with every type. Tiles are drawn with CSS (no thumbnail
// downloads), so the picker works offline. A collapsed legend chip sits beside it.
import type { BaseLayers } from "./basemap";
import { h } from "./dom";
import { icon } from "./icons";

/** Swatch class per base map id (style.css draws each one). */
const SWATCH: Record<string, string> = {
  offline: "mt-offline",
  dark: "mt-dark",
  light: "mt-light",
  liberty: "mt-detailed",
  bright: "mt-bright",
  satellite: "mt-satellite",
  "blank-dark": "mt-dark",
  "blank-light": "mt-light",
};
const SHORT: Record<string, string> = {
  offline: "Default",
  dark: "Dark",
  light: "Light",
  liberty: "Detailed",
  bright: "Bright",
  satellite: "Satellite",
  "blank-dark": "Blank dark",
  "blank-light": "Blank light",
};

export function mountMapType(root: HTMLElement, layers: BaseLayers, more: () => void, extras: HTMLElement[] = []): void {
  const tile = (id: string, label: string, cls = ""): HTMLElement =>
    h("span", { class: `mt-tile ${SWATCH[id] ?? "mt-offline"} ${cls}`.trim() }, h("span", { class: "mt-name" }, label));
  const draw = (): void => {
    const all = layers.list();
    const cur = layers.currentId();
    // the main tile previews the next type (like Google Maps: "Satellite" while on the map)
    const alt = all.find((b) => b.id === "satellite" && b.id !== cur) ?? all.find((b) => b.id !== cur) ?? all[0]!;
    const main = h(
      "button",
      { type: "button", class: "mt-main", title: `Map type: switch to ${SHORT[alt.id] ?? alt.label}`, "aria-label": `Map type (now ${SHORT[cur] ?? cur}). Switch to ${SHORT[alt.id] ?? alt.label}`, onclick: () => layers.setBase(alt.id) },
      tile(alt.id, SHORT[alt.id] ?? alt.label),
      h("span", { class: "mt-caption" }, icon("layers", 14), "Layers"),
    );
    const row = h(
      "div",
      { class: "mt-row", role: "group", "aria-label": "Map types" },
      ...all.map((b) =>
        h(
          "button",
          { type: "button", class: "mt-opt", "aria-pressed": String(b.id === cur), title: b.thirdParty ? `${b.label} (third-party tiles)` : b.label, onclick: () => layers.setBase(b.id) },
          tile(b.id, SHORT[b.id] ?? b.label),
        ),
      ),
      h("button", { type: "button", class: "mt-opt mt-more", title: "Overlays and online map styles (Settings)", onclick: more }, h("span", { class: "mt-tile mt-moretile" }, icon("settings", 18), h("span", { class: "mt-name" }, "More"))),
    );
    // hover/focus opens the row of types; the Filters button sits outside that hover group
    root.replaceChildren(h("div", { class: "mt-wrap" }, main, row), ...extras);
  };
  layers.onListChange = draw;
  draw();
}

/** Minimised legend: a small chip; click to open the key above it. */
export function mountLegend(root: HTMLElement, body: HTMLElement): void {
  const btn = h("button", { type: "button", class: "legend-chip", "aria-expanded": "false", "aria-controls": "legend-pop", title: "Map key" }, icon("help", 14), "Legend", icon("up", 14));
  const pop = h("div", { id: "legend-pop", class: "legend-pop", hidden: true, role: "region", "aria-label": "Map key" }, body);
  btn.addEventListener("click", () => {
    pop.hidden = !pop.hidden;
    btn.setAttribute("aria-expanded", String(!pop.hidden));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !pop.hidden) {
      pop.hidden = true;
      btn.setAttribute("aria-expanded", "false");
    }
  });
  root.replaceChildren(pop, btn);
}
