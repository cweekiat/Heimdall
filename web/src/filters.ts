// Layer toggles and quick filters for live traffic. State persists in localStorage.
import type { AirProps, SeaProps, SourceState } from "./api";
import { h } from "./dom";
import { icon } from "./icons";
import type { IconName } from "./icons";
import { COVERAGE_NOTE } from "./trafficdata";
import { T } from "./labels";
import { ROLE_LABEL, ROLE_ORDER, roleCss } from "./milroles";
import { P3_DEFAULT_ON } from "./p3model";
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
  /** 046: military roles shown (tanker, isr, ...). Empty = no role filter. Any role picked shows military only. */
  milRoles: string[];
  /** 046: "Hide others" from the aircraft list: only these ICAO hexes. Empty = off. */
  onlyHexes: string[];
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
  /** Satellites and their ground tracks (globe and map). */
  satellites: boolean;
  /** Satellite groups hidden ("GNSS", "Space station", "Earth observation"). */
  hiddenSatGroups: string[];
  /** Alert types (detectors) hidden from the map. */
  hiddenKinds: string[];
  /** Data-quality coverage cells (thin data is greyed). */
  coverage: boolean;
}

const KEY = "hm.filters.v1";
const DEFAULTS: Filters = {
  air: true,
  sea: true,
  alerts: true,
  notices: true,
  milOnly: false,
  milRoles: [],
  onlyHexes: [],
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
  satellites: true,
  hiddenKinds: [],
  hiddenSatGroups: [],
  coverage: false,
};

function load(): Filters {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<Filters>;
    // display filters only: source on/off lives in Data sources; an old "hidden sources" choice
    // made here would silently hide data, so it is not carried over
    return { ...DEFAULTS, ...raw, hiddenSources: [] };
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
  if (filters.milRoles.length > 0 && !(p.mil && filters.milRoles.includes(p.role ?? "other"))) return false;
  if (filters.onlyHexes.length > 0 && !filters.onlyHexes.includes(p.hex.toLowerCase())) return false;
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
  if (filters.onlyHexes.length > 0) return false; // "Hide others" keeps only the picked aircraft
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
  /** Loaded aircraft / ships in view ({air, sea}). */
  counts: () => Record<string, number>;
  /** Satellites tracked. */
  satellites: () => number;
  /** Open alerts per alert kind. */
  alertKinds: () => Record<string, number>;
  /** Extra controls under a switched-on spectrum layer (GNSS date, HF band). */
  spectrumExtra: (l: SpectrumLayer) => HTMLElement | null;
  /** Short status of a spectrum layer ("3 open", newest data date). */
  spectrumStatus: (l: SpectrumLayer) => string;
  /** Next-pass chips under "Next satellite passes". */
  extra?: () => HTMLElement;
}

/** Detectors whose alerts the Detections group can show or hide (order = list order). */
export const DETECTIONS: { kind: string; label: string; icon: IconName }[] = [
  { kind: "gnss_jamming", label: "GPS jamming", icon: "radio" },
  { kind: "gnss_spoofing", label: "GPS spoofing", icon: "gps" },
  { kind: "mil_orbit", label: "Military aircraft circling", icon: "plane" },
  { kind: "ais_gap", label: "Ship went dark (AIS off)", icon: "ship" },
  { kind: "ais_spoofing", label: "Ship position faked", icon: "ship" },
  { kind: "spectrum_anomaly", label: "Radio band far from normal", icon: "activity" },
  { kind: "warning_area", label: "Activity in a warning area", icon: "notice" },
  { kind: "thermal", label: "Heat / fire cluster", icon: "warn" },
  { kind: "composite", label: "Several signs in one place", icon: "alerts" },
];

interface Row {
  id: string;
  icon: IconName;
  label: string;
  /** Data source, shown in brackets: "Aircraft (ADS-B)". */
  src?: string;
  on: () => boolean;
  set: (on: boolean) => void;
  count?: () => string;
  extra?: () => HTMLElement | null;
  /** Indented sub-option of the row above. */
  sub?: boolean;
}
interface Group {
  id: string;
  title: string;
  rows: Row[];
}

const OPEN_KEY = "hm.filters.open";
const toggleIn = <T,>(xs: T[], x: T, on: boolean): T[] => (on ? [...xs.filter((y) => y !== x), x] : xs.filter((y) => y !== x));

/** The filter page (Google-Earth-style layer list). Every row only shows or hides data on the map;
 * nothing here changes what the sources collect. Returns refresh() for counts. */
export function mountFilters(root: HTMLElement, ctx: PanelCtx, lookBack: HTMLElement): () => void {
  let open: Record<string, boolean> = { traffic: true, spectrum: true, earth: false, notices: false, detections: true, quick: false };
  try {
    open = { ...open, ...(JSON.parse(localStorage.getItem(OPEN_KEY) ?? "{}") as Record<string, boolean>) };
  } catch {
    /* defaults */
  }
  const fmt = (n: number): string => (n ? n.toLocaleString() : "");
  const spec = (l: SpectrumLayer, icon: IconName, label: string, src: string): Row => ({
    id: `s-${l}`,
    icon,
    label,
    src,
    on: () => filters.spectrum.includes(l),
    set: (on) => setFilter({ spectrum: toggleIn(filters.spectrum, l, on) }),
    count: () => ctx.spectrumStatus(l),
    extra: () => ctx.spectrumExtra(l),
  });
  const p3 = (l: P3Layer, icon: IconName, label: string, src: string, extra?: () => HTMLElement | null): Row => ({
    id: `p-${l}`,
    icon,
    label,
    src,
    on: () => filters.p3.includes(l),
    set: (on) => setFilter({ p3: toggleIn(filters.p3, l, on) }),
    extra,
  });
  const flag = (k: "air" | "sea" | "satellites" | "alerts" | "notices" | "gnssNoticesOnly" | "coverage", icon: IconName, label: string, src?: string, count?: () => string): Row => ({
    id: `f-${k}`,
    icon,
    label,
    src,
    on: () => filters[k],
    set: (on) => setFilter({ [k]: on }),
    count,
  });
  const groups = (): Group[] => [
    {
      id: "traffic",
      title: "Traffic",
      rows: [
        flag("air", "plane", "Aircraft", "ADS-B", () => fmt(ctx.counts().air ?? 0)),
        flag("sea", "ship", "Vessels", "AIS", () => fmt(ctx.counts().sea ?? 0)),
        flag("satellites", "orbit", "Satellites", "CelesTrak", () => fmt(ctx.satellites())),
        ...(filters.satellites
          ? [
              ["Earth observation", "Earth-observation satellites"],
              ["GNSS", "GNSS satellites (GPS, Galileo, GLONASS, BeiDou)"],
              ["Space station", "Space stations"],
            ].map(([g, label]): Row => ({
              id: `g-${g}`,
              icon: "orbit",
              label: label!,
              sub: true,
              on: () => !filters.hiddenSatGroups.includes(g!),
              set: (on) => setFilter({ hiddenSatGroups: toggleIn(filters.hiddenSatGroups, g!, !on) }),
            }))
          : []),
      ],
    },
    {
      id: "spectrum",
      title: "GNSS and radio spectrum",
      rows: [
        spec("gnss", "gps", "GPS interference map", "ADS-B accuracy"),
        spec("jammers", "ring", "Estimated jammer locations", "ADS-B"),
        spec("sar_rfi", "radio", "C-band interference", "Sentinel-1"),
        spec("smap", "radio", "L-band interference", "SMAP"),
        spec("hf", "activity", "HF propagation", "WSPR, PSK Reporter"),
        spec("satnogs", "orbit", "Satellite downlinks", "SatNOGS"),
        spec("coverage", "coverage", "Receivers heard (pick a band)", "live receivers"),
      ],
    },
    {
      id: "earth",
      title: "Earth observation",
      rows: [
        p3("thermal", "warn", "Heat spots", "NASA FIRMS"),
        p3("overpass", "orbit", "Next satellite passes", "CelesTrak", () => (filters.p3.includes("overpass") ? (ctx.extra?.() ?? null) : null)),
      ],
    },
    {
      id: "notices",
      title: "Alerts and notices",
      rows: [
        flag("alerts", "alerts", "Alert markers", "detectors"),
        flag("notices", "notice", "Warnings and NOTAMs", "NGA, FAA"),
        flag("gnssNoticesOnly", "gps", "Only GPS-interference notices"),
        flag("coverage", "coverage", "Data coverage (thin data greyed)"),
      ],
    },
    {
      id: "detections",
      title: "Detections",
      rows: DETECTIONS.map((d) => ({
        id: `k-${d.kind}`,
        icon: d.icon,
        label: d.label,
        on: () => !filters.hiddenKinds.includes(d.kind),
        set: (on: boolean) => setFilter({ hiddenKinds: toggleIn(filters.hiddenKinds, d.kind, !on) }),
        count: () => {
          const n = ctx.alertKinds()[d.kind] ?? 0;
          return n ? `${n} open` : "";
        },
      })),
    },
  ];

  // ---- quick filters: built once (typing must not lose focus on refresh)
  const chk = (label: string, key: "milOnly" | "onlyAlerts" | "gpsBadOnly"): HTMLElement => {
    const i = h("input", { type: "checkbox" });
    i.checked = filters[key];
    i.dataset.quick = "1";
    i.addEventListener("change", () => setFilter({ [key]: i.checked }));
    return h("label", { class: "chk" }, i, label);
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
          { type: "button", class: "chip", "aria-pressed": String(on), onclick: () => (setFilter({ shipClasses: toggleIn(filters.shipClasses, c, !on) }), drawChips()) },
          T.shipClass[c] ?? c,
        );
      }),
    );
  drawChips();
  const roleChips = h("div", { class: "chips" });
  const drawRoleChips = (): void =>
    roleChips.replaceChildren(
      ...ROLE_ORDER.map((r) => {
        const on = filters.milRoles.includes(r);
        return h(
          "button",
          { type: "button", class: "chip role-chip", "aria-pressed": String(on), onclick: () => setFilter({ milRoles: toggleIn(filters.milRoles, r, !on) }) },
          h("i", { class: "role-dot", style: `background:${roleCss(r)}` }),
          ROLE_LABEL[r] ?? r,
        );
      }),
    );
  drawRoleChips();
  const search = h("input", { type: "search", placeholder: T.searchTraffic, "aria-label": T.searchTraffic, id: "traffic-q" });
  search.value = filters.search;
  search.addEventListener("input", () => setFilter({ search: search.value }));
  const quick = h(
    "div",
    { class: "fl-quick" },
    search,
    chk(T.militaryOnly, "milOnly"),
    chk(T.onlyInAlerts, "onlyAlerts"),
    chk("Only aircraft with GPS problems", "gpsBadOnly"),
    h("div", { class: "alt" }, h("span", {}, T.altitudeBand), altLo, altHi, altTxt),
    h("div", {}, h("span", {}, "Military aircraft types (none picked = all aircraft)"), roleChips),
    h("div", {}, h("span", {}, T.shipTypes), chips),
    lookBack,
    h(
      "button",
      {
        type: "button",
        onclick: () => {
          setFilter({ milOnly: false, milRoles: [], onlyHexes: [], altMin: 0, altMax: ALT_MAX, shipClasses: [], onlyAlerts: false, gpsBadOnly: false, search: "" });
          altLo.value = "0";
          altHi.value = String(ALT_MAX);
          search.value = "";
          showAlt();
          drawChips();
          quick.querySelectorAll<HTMLInputElement>("input[data-quick]").forEach((i) => (i.checked = false));
        },
      },
      T.clearFilters,
    ),
  );

  const head = h("div", { class: "fl-head" });
  const list = h("div", { class: "fl-list" });
  const saveOpen = (): void => {
    try {
      localStorage.setItem(OPEN_KEY, JSON.stringify(open));
    } catch {
      /* private mode */
    }
  };
  const groupHead = (id: string, title: string, n: number, total: number, allOff?: () => void): HTMLElement =>
    h(
      "div",
      { class: "fl-group-head" },
      h(
        "button",
        { type: "button", class: "fl-toggle", "aria-expanded": String(!!open[id]), onclick: () => ((open[id] = !open[id]), saveOpen(), draw()) },
        icon(open[id] ? "down" : "right", 14),
        h("span", {}, title),
      ),
      h("span", { class: "note num" }, total ? `${n}/${total}` : ""),
      allOff && n ? h("button", { type: "button", class: "fl-alloff", onclick: allOff }, "All off") : null,
    );
  const draw = (): void => {
    const gs = groups();
    const nOn = gs.reduce((a, g) => a + g.rows.filter((r) => r.on()).length, 0);
    head.replaceChildren(h("div", {}, h("strong", {}, "Layers"), h("div", { class: "note" }, `${nOn} on · only changes what you see`)));
    list.replaceChildren(
      ...gs.map((g) => {
        const n = g.rows.filter((r) => r.on()).length;
        return h(
          "section",
          { class: "fl-group" },
          groupHead(g.id, g.title, n, g.rows.length, () => g.rows.forEach((r) => r.on() && r.set(false))),
          open[g.id]
            ? h(
                "ul",
                { class: "fl-rows" },
                ...g.rows.flatMap((r) => {
                  const on = r.on();
                  const extra = on ? (r.extra?.() ?? null) : null;
                  return [
                    h(
                      "li",
                      {},
                      h(
                        "button",
                        { type: "button", class: `fl-row${r.sub ? " sub" : ""}`, role: "switch", "aria-checked": String(on), onclick: () => r.set(!on) },
                        icon(r.icon, 18),
                        h("span", { class: "fl-label" }, r.label, r.src ? h("span", { class: "fl-src" }, ` (${r.src})`) : null),
                        h("span", { class: "fl-count" }, r.count?.() || (on ? "" : "Off")),
                      ),
                      extra ? h("div", { class: "fl-extra" }, extra) : null,
                    ),
                  ];
                }),
              )
            : null,
        );
      }),
      h(
        "section",
        { class: "fl-group" },
        groupHead("quick", T.quickFilters, 0, 0),
        open.quick ? quick : null,
      ),
      h("p", { class: "note coverage-note" }, COVERAGE_NOTE),
    );
  };
  root.replaceChildren(head, list);
  draw();
  onFilters(() => {
    if (document.activeElement !== search && search.value !== filters.search) search.value = filters.search;
    drawRoleChips();
    draw();
  });
  return draw;
}
