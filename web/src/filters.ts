// Layer toggles and quick filters for live traffic. State persists in localStorage.
import type { AirProps, SeaProps, SourceState } from "./api";
import { h } from "./dom";
import { icon } from "./icons";
import type { IconName } from "./icons";
import { COVERAGE_NOTE } from "./trafficdata";
import { T } from "./labels";
import { P3_DEFAULT_ON, P3_LABEL, P3_LAYERS } from "./p3model";
import type { P3Layer } from "./p3model";
import { SPECTRUM_DEFAULT_ON } from "./spectrummodel";
import type { SpectrumLayer } from "./spectrummodel";

export const SHIP_CLASSES = ["cargo", "tanker", "passenger", "fishing", "military", "other"] as const;
export const ALT_MAX = 45000;

export interface Filters {
  air: boolean;
  sea: boolean;
  alerts: boolean;
  notices: boolean;
  milOnly: boolean;
  altMin: number;
  altMax: number;
  /** Ship types shown. Empty = all. */
  shipClasses: string[];
  onlyAlerts: boolean;
  search: string;
  /** Source keys the analyst unticked. */
  hiddenSources: string[];
  /** P3 satellite layers switched on (heat spots by default). */
  p3: P3Layer[];
  /** Spectrum layers switched on (task 030; GNSS daily map by default). */
  spectrum: SpectrumLayer[];
  /** GNSS map date: "last24h" or YYYY-MM-DD. */
  gnssDate: string;
  /** Band filter from the band ladder ("" = all bands). */
  band: string;
  /** Only aircraft whose GPS accuracy is degraded. */
  gpsBadOnly: boolean;
  /** Notices layer: only official GNSS-interference notices. */
  gnssNoticesOnly: boolean;
  /** HF spots: one amateur band in MHz, 0 = all. */
  hfBand: number;
}

const KEY = "hm.filters.v1";
const DEFAULTS: Filters = {
  air: true,
  sea: true,
  alerts: true,
  notices: true,
  milOnly: false,
  altMin: 0,
  altMax: ALT_MAX,
  shipClasses: [],
  onlyAlerts: false,
  search: "",
  hiddenSources: [],
  p3: [...P3_DEFAULT_ON],
  spectrum: [...SPECTRUM_DEFAULT_ON],
  gnssDate: "last24h",
  band: "",
  gpsBadOnly: false,
  gnssNoticesOnly: false,
  hfBand: 0,
};

function load(): Filters {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<Filters>;
    return { ...DEFAULTS, ...raw };
  } catch {
    return { ...DEFAULTS };
  }
}

export const filters: Filters = load();
const listeners: (() => void)[] = [];
export const onFilters = (fn: () => void): void => void listeners.push(fn);

export function setFilter(p: Partial<Filters>): void {
  Object.assign(filters, p);
  try {
    localStorage.setItem(KEY, JSON.stringify(filters));
  } catch {
    /* storage unavailable */
  }
  listeners.forEach((f) => f());
}

const altFilterOn = (): boolean => filters.altMin > 0 || filters.altMax < ALT_MAX;

export function matchAir(p: AirProps): boolean {
  if (filters.milOnly && !p.mil) return false;
  if (filters.onlyAlerts && !p.in_alert) return false;
  if (filters.gpsBadOnly && !p.gps_bad) return false;
  if (altFilterOn()) {
    const alt = p.on_ground ? 0 : p.alt_baro;
    if (alt === null || alt < filters.altMin || alt > filters.altMax) return false;
  }
  const q = filters.search.trim().toLowerCase();
  if (q && ![p.flight, p.hex, p.registration].some((v) => v?.toLowerCase().includes(q))) return false;
  return true;
}

export function matchSea(p: SeaProps): boolean {
  if (filters.milOnly && p.ship_class !== "military") return false;
  if (filters.onlyAlerts && !p.in_alert) return false;
  if (filters.shipClasses.length > 0 && !filters.shipClasses.includes(p.ship_class)) return false;
  const q = filters.search.trim().toLowerCase();
  if (q && !(p.name?.toLowerCase().includes(q) || String(p.mmsi).includes(q))) return false;
  return true;
}

/** Sources of one domain the analyst has left ticked. Undefined = all of them (no filter needed). */
export function activeSources(all: SourceState[], domain: string): string[] | undefined {
  const of = all.filter((s) => s.domain === domain);
  const on = of.filter((s) => !filters.hiddenSources.includes(s.key));
  return on.length === of.length ? undefined : on.map((s) => s.key);
}

export interface PanelCtx {
  counts: () => Record<string, number>;
  sources: () => SourceState[];
  /** Optional extra block under the satellite toggles (next-pass chips). */
  extra?: () => HTMLElement;
  /** Spectrum toggles for the Live and Daily sweep groups (task 030). */
  live?: () => HTMLElement;
  daily?: () => HTMLElement;
}

type ToggleKey = "air" | "sea" | "alerts" | "notices" | "milOnly" | "onlyAlerts" | "gpsBadOnly" | "gnssNoticesOnly";

/** Build the filter panel once; call the returned refresh() to update source rows and counts. */
export function mountFilters(root: HTMLElement, ctx: PanelCtx): () => void {
  const srcBox = h("div", { class: "src-list" });
  const chk = (label: string, key: ToggleKey, ico?: IconName, quick = false): HTMLElement => {
    const i = h("input", { type: "checkbox" });
    i.checked = filters[key];
    if (quick) i.dataset.quick = "1";
    i.addEventListener("change", () => setFilter({ [key]: i.checked }));
    return h("label", { class: "chk" }, i, ico ? icon(ico, 14) : null, label);
  };
  const altLo = h("input", { type: "range", min: "0", max: String(ALT_MAX), step: "1000", "aria-label": T.altLow });
  const altHi = h("input", { type: "range", min: "0", max: String(ALT_MAX), step: "1000", "aria-label": T.altHigh });
  altLo.value = String(filters.altMin);
  altHi.value = String(filters.altMax);
  const altTxt = h("span", { class: "note" });
  const showAlt = (): void => {
    altTxt.textContent = `${Number(altLo.value).toLocaleString()} to ${Number(altHi.value).toLocaleString()} ft`;
  };
  const onAlt = (): void => {
    let lo = Number(altLo.value);
    let hi = Number(altHi.value);
    if (lo > hi) [lo, hi] = [hi, lo];
    showAlt();
    setFilter({ altMin: lo, altMax: hi });
  };
  altLo.addEventListener("input", onAlt);
  altHi.addEventListener("input", onAlt);
  showAlt();
  const chips = h("div", { class: "chips" });
  const drawChips = (): void =>
    chips.replaceChildren(
      ...SHIP_CLASSES.map((c) => {
        const on = filters.shipClasses.includes(c);
        return h(
          "button",
          {
            type: "button",
            class: "chip",
            "aria-pressed": String(on),
            onclick: () => {
              setFilter({ shipClasses: on ? filters.shipClasses.filter((x) => x !== c) : [...filters.shipClasses, c] });
              drawChips();
            },
          },
          T.shipClass[c] ?? c,
        );
      }),
    );
  drawChips();
  const search = h("input", { type: "search", placeholder: T.searchTraffic, "aria-label": T.searchTraffic });
  search.value = filters.search;
  search.addEventListener("input", () => setFilter({ search: search.value }));
  const reset = h(
    "button",
    {
      type: "button",
      onclick: () => {
        setFilter({ milOnly: false, altMin: 0, altMax: ALT_MAX, shipClasses: [], onlyAlerts: false, gpsBadOnly: false, search: "" });
        altLo.value = "0";
        altHi.value = String(ALT_MAX);
        search.value = "";
        showAlt();
        drawChips();
        root.querySelectorAll<HTMLInputElement>("input[data-quick]").forEach((i) => (i.checked = false));
      },
    },
    T.clearFilters,
  );
  const p3chk = (l: P3Layer): HTMLElement => {
    const i = h("input", { type: "checkbox" });
    i.checked = filters.p3.includes(l);
    i.addEventListener("change", () => setFilter({ p3: i.checked ? [...filters.p3.filter((x) => x !== l), l] : filters.p3.filter((x) => x !== l) }));
    return h("label", { class: "chk" }, i, P3_LABEL[l]);
  };
  root.replaceChildren(
    h(
      "fieldset",
      {},
      h("legend", {}, "Live"),
      chk(T.aircraft, "air", "plane"),
      chk(T.ships, "sea", "ship"),
      chk(T.alertMarkers, "alerts", "alerts"),
      chk(T.noticesLayer, "notices", "notice"),
      chk("Only official GPS-interference notices", "gnssNoticesOnly"),
      ctx.live?.() ?? null,
      h("p", { class: "note coverage-note" }, COVERAGE_NOTE),
    ),
    h(
      "fieldset",
      {},
      h("legend", {}, "Daily sweep"),
      h("p", { class: "note" }, "Hours to a day old. Each layer shows the date of its data."),
      ctx.daily?.() ?? null,
      ...P3_LAYERS.map(p3chk),
      ctx.extra?.() ?? null,
    ),
    h("fieldset", {}, h("legend", {}, T.sourcesToShow), srcBox),
    h(
      "fieldset",
      {},
      h("legend", {}, T.quickFilters),
      search,
      chk(T.militaryOnly, "milOnly", undefined, true),
      chk(T.onlyInAlerts, "onlyAlerts", undefined, true),
      chk("Only aircraft with GPS problems", "gpsBadOnly", undefined, true),
      h("div", { class: "alt" }, h("span", {}, T.altitudeBand), altLo, altHi, altTxt),
      h("div", {}, h("span", {}, T.shipTypes), chips),
      reset,
    ),
  );
  return (): void => {
    const c = ctx.counts();
    const rows = ctx.sources().filter((s) => s.domain === "air" || s.domain === "sea");
    srcBox.replaceChildren(
      ...rows.map((s) => {
        const i = h("input", { type: "checkbox" });
        i.checked = !filters.hiddenSources.includes(s.key);
        i.addEventListener("change", () =>
          setFilter({ hiddenSources: i.checked ? filters.hiddenSources.filter((k) => k !== s.key) : [...filters.hiddenSources, s.key] }),
        );
        return h("label", { class: "chk" }, i, icon(s.domain === "air" ? "plane" : "ship", 14), s.label, h("span", { class: "note num" }, ` ${c[s.key] ?? 0}`));
      }),
      ...(rows.length === 0 ? [h("span", { class: "note" }, T.noSources)] : []),
    );
  };
}
