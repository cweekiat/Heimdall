// Spectrum model: bands, layer groups, GNSS map colours, hover text.
// Pure functions, no deck.gl or DOM, so they run under node --test. All strings end up in textContent.
import { fmtDT } from "./tz.ts";
import type { LayerFeature } from "./p3model.ts";

// ------------------------------------------------------------------ bands (ladder, frequency order)

export type Band = "hf" | "vhf_marine" | "sat_downlink" | "l_1090" | "l1_gnss" | "l_1400" | "c_sar";
export interface BandDef {
  band: Band;
  name: string;
  range: string;
  /** Source keys that feed this band (for the status dot and hover). */
  sources: string[];
}
export const BANDS: BandDef[] = [
  { band: "hf", name: "HF", range: "3–30 MHz", sources: ["wspr_live", "pskreporter"] },
  { band: "vhf_marine", name: "VHF marine", range: "156–162 MHz", sources: ["aisstream"] },
  { band: "sat_downlink", name: "Sat downlinks", range: "VHF/UHF/S", sources: ["satnogs"] },
  { band: "l_1090", name: "L 1090", range: "1090 MHz", sources: ["adsb_lol", "adsb_fi", "opensky_global"] },
  { band: "l1_gnss", name: "L1 GNSS", range: "1559–1610 MHz", sources: ["adsb_lol", "adsb_fi", "aisstream", "faa_nms_notam", "nga_msi", "cygnss"] },
  { band: "l_1400", name: "L 1.4 GHz", range: "1400–1427 MHz", sources: ["smap_rfi"] },
  { band: "c_sar", name: "C SAR", range: "~5.4 GHz", sources: ["sentinel1_rfi"] },
];
export const BAND_NAME: Record<string, string> = Object.fromEntries(BANDS.map((b) => [b.band, b.name]));

export type Dot = "grey" | "amber" | "green";
/** grey = no source for this band yet, amber = a source is unhealthy / needs a key, green = receiving. */
export function bandDot(def: BandDef, sources: { key: string; status: string; enabled?: boolean }[]): Dot {
  const mine = sources.filter((s) => def.sources.includes(s.key) && s.enabled !== false);
  if (!mine.length) return "grey";
  if (mine.some((s) => s.status === "ok")) return mine.every((s) => s.status === "ok") ? "green" : "amber";
  return "amber";
}

/** Band filter: alerts carry `bands`; older alerts without it never match a band. */
export const inBand = (a: { bands?: string[] | null }, band: string): boolean => !band || (a.bands ?? []).includes(band);

export function bandCounts(alerts: { bands?: string[] | null }[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const a of alerts) for (const b of a.bands ?? []) out[b] = (out[b] ?? 0) + 1;
  return out;
}

// ------------------------------------------------------------------ hash deep link

export function readHash(hash: string): { band: string; gnss: string } {
  const p = new URLSearchParams(hash.replace(/^#/, ""));
  const band = p.get("band") ?? "";
  const gnss = p.get("gnss") ?? "";
  return {
    band: BANDS.some((b) => b.band === band) ? band : "",
    gnss: gnss === "last24h" || /^\d{4}-\d{2}-\d{2}$/.test(gnss) ? gnss : "",
  };
}

export function writeHash(hash: string, patch: { band?: string; gnss?: string }): string {
  const p = new URLSearchParams(hash.replace(/^#/, ""));
  for (const [k, v] of Object.entries(patch)) v ? p.set(k, v) : p.delete(k);
  const s = p.toString();
  return s ? `#${s}` : "";
}

// ------------------------------------------------------------------ spectrum layers

export type SpectrumLayer = "gnss" | "hf" | "satnogs" | "sar_rfi" | "smap" | "cygnss" | "jammers" | "coverage";
export const SPECTRUM_LAYERS: SpectrumLayer[] = ["jammers", "coverage", "hf", "satnogs", "gnss", "cygnss", "sar_rfi", "smap"];
export const LIVE_LAYERS: SpectrumLayer[] = ["jammers", "coverage", "hf", "satnogs"];
export const DAILY_LAYERS: SpectrumLayer[] = ["gnss", "cygnss", "sar_rfi", "smap"];
export const SPECTRUM_LABEL: Record<SpectrumLayer, string> = {
  jammers: "Estimated jammer locations",
  coverage: "Receivers heard (band picked in the band panel)",
  hf: "HF propagation spots (WSPR, PSK Reporter)",
  satnogs: "Satellite passes (SatNOGS ground stations)",
  gnss: "GNSS interference (daily, GPSJam method)",
  sar_rfi: "C-band interference (Sentinel-1)",
  smap: "L-band interference (SMAP)",
  cygnss: "GPS noise seen from orbit (CYGNSS, daily)",
};
/** Hours of data requested per layer. */
export type EventLayer = "hf" | "satnogs" | "sar_rfi" | "smap";
export const SPECTRUM_HOURS: Record<EventLayer, number> = { hf: 1, satnogs: 6, sar_rfi: 72, smap: 48 };
export const SPECTRUM_REFRESH_MS: Record<SpectrumLayer, number> = {
  jammers: 60_000,
  coverage: 60_000,
  hf: 120_000,
  satnogs: 300_000,
  gnss: 300_000,
  sar_rfi: 600_000,
  smap: 600_000,
  cygnss: 1_800_000,
};
export const SPECTRUM_DEFAULT_ON: SpectrumLayer[] = ["gnss", "jammers"];
/** Bands the coverage layer can show (receivers the live index knows about). */
export const COVERAGE_BANDS = ["l1_gnss", "l_1090", "vhf_marine"];
export const HF_BANDS_MHZ = [3, 5, 7, 10, 14, 18, 21, 24, 28];
/** HF spot opacity from median SNR: -28 dB (barely decoded) .. +5 dB (strong) -> 0.15 .. 0.95. */
export const hfAlpha = (snr: number | null): number => (snr === null ? 0.4 : Math.min(0.95, Math.max(0.15, 0.15 + (0.8 * (snr + 28)) / 33)));

// ------------------------------------------------------------------ GNSS day map

export type GnssClass = "green" | "yellow" | "red";
export interface GnssCell {
  h3: string;
  good: number;
  bad: number;
  total: number;
  pct: number;
  cls: GnssClass;
}
export interface GnssDay {
  date: string;
  incomplete: boolean;
  hours_with_data: number;
  cells: GnssCell[];
}
/** GPSJam's own legend colours: a scientific scale, so it is not mapped onto the alert palette. */
export const GNSS_RGB: Record<GnssClass, [number, number, number]> = { green: [60, 170, 90], yellow: [225, 190, 40], red: [220, 60, 50] };
export const GNSS_LEGEND: [GnssClass, string][] = [
  ["green", "< 2 % aircraft degraded"],
  ["yellow", "2–10 %"],
  ["red", "> 10 %"],
];

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string => (typeof v === "string" ? v : "");

export function parseGnssDay(raw: unknown): GnssDay | null {
  if (!isObj(raw) || !Array.isArray(raw.cells)) return null;
  const cells: GnssCell[] = [];
  for (const c of raw.cells) {
    if (!isObj(c) || typeof c.h3 !== "string" || !/^[0-9a-f]{15}$/.test(c.h3)) continue;
    const cls = c.cls === "red" || c.cls === "yellow" ? c.cls : "green";
    cells.push({ h3: c.h3, good: num(c.good) ?? 0, bad: num(c.bad) ?? 0, total: num(c.total) ?? 0, pct: num(c.pct) ?? 0, cls });
  }
  return { date: str(raw.date), incomplete: raw.incomplete === true, hours_with_data: num(raw.hours_with_data) ?? 0, cells };
}

export const gnssHover = (c: GnssCell): string =>
  `GNSS: ${c.bad} of ${c.total} aircraft degraded · ${c.pct}% after the −1 de-noise · ${c.cls}`;

export function gnssDateLabel(d: GnssDay | null): string {
  if (!d) return "No GNSS map yet";
  const what = d.date === "last24h" ? "last 24 h" : `data for ${d.date} UTC`;
  return `${what}${d.incomplete ? ` · incomplete (${d.hours_with_data}/24 h)` : ""}`;
}

/** Previous / next day in a sorted list of available days ("last24h" sits after the newest day). */
export function stepDay(days: string[], cur: string, dir: -1 | 1): string {
  const all = [...days, "last24h"];
  const i = all.indexOf(cur);
  const j = (i < 0 ? all.length - 1 : i) + dir;
  return all[Math.max(0, Math.min(all.length - 1, j))]!;
}

// ------------------------------------------------------------------ CYGNSS L1 noise day map

export interface CygnssCell {
  h3: string;
  excess: number;
  delta: number | null;
  z: number | null;
  n: number;
  sats: number;
  ocean: number;
  base: number | null;
  baseDays: number;
  flag: "level" | "change" | null;
}
export interface CygnssDay {
  date: string;
  total: number;
  cells: CygnssCell[];
  levelDb: number;
  landLevelDb: number;
  changeDb: number;
  limits: string;
}

export function parseCygnssDay(raw: unknown): CygnssDay | null {
  if (!isObj(raw) || !Array.isArray(raw.cells)) return null;
  const th = isObj(raw.thresholds) ? raw.thresholds : {};
  const cells: CygnssCell[] = [];
  for (const c of raw.cells) {
    if (!isObj(c) || typeof c.h3 !== "string" || !/^[0-9a-f]{15}$/.test(c.h3)) continue;
    cells.push({
      h3: c.h3,
      excess: num(c.excess_db) ?? 0,
      delta: num(c.delta_db),
      z: num(c.z),
      n: num(c.n) ?? 0,
      sats: num(c.n_sats) ?? 0,
      ocean: num(c.ocean_frac) ?? 0,
      base: num(c.base_db),
      baseDays: num(c.base_days) ?? 0,
      flag: c.flag === "level" || c.flag === "change" ? c.flag : null,
    });
  }
  return {
    date: str(raw.date),
    total: num(raw.total_cells) ?? cells.length,
    cells,
    levelDb: num(th.level_db) ?? 2.5,
    landLevelDb: num(th.land_level_db) ?? 3.5,
    changeDb: num(th.change_db) ?? 1.5,
    limits: str(raw.limits),
  };
}

/** 0..1 how close a cell is to the detector's thresholds (level for its surface, or rise). */
export function cygnssStrength(c: CygnssCell, d: Pick<CygnssDay, "levelDb" | "landLevelDb" | "changeDb">): number {
  const level = c.ocean >= 0.9 ? d.levelDb : d.landLevelDb;
  const a = c.excess / level;
  const b = c.delta !== null ? c.delta / d.changeDb : 0;
  return Math.max(0, Math.min(1, Math.max(a, b)));
}

export function cygnssHover(c: CygnssCell): string {
  const rise = c.delta !== null ? ` · ${c.delta >= 0 ? "+" : ""}${c.delta.toFixed(1)} dB vs its usual ${c.base?.toFixed(1) ?? "?"} dB (${c.baseDays} days)` : " · no baseline yet";
  const flag = c.flag === "level" ? " · above the alert level" : c.flag === "change" ? " · unusual rise for this cell" : "";
  const where = c.ocean >= 0.9 ? "open ocean" : c.ocean > 0.1 ? "coast" : "land";
  return `CYGNSS GPS noise ${c.excess.toFixed(1)} dB above open-ocean normal${rise}${flag} · ${where}, ${c.n} reflections, ${c.sats} satellite${c.sats === 1 ? "" : "s"}`;
}

export const CYGNSS_LEGEND: [string, string][] = [
  ["cy-low", "slightly raised"],
  ["cy-mid", "near alert level"],
  ["cy-flag", "meets alert rules"],
];

// ------------------------------------------------------------------ hover text for event layers

export function spectrumHover(layer: SpectrumLayer, f: LayerFeature): string {
  const p = f.properties;
  const t = p.ts ? fmtDT(p.ts) : "";
  switch (layer) {
    case "hf": {
      const snr = num(p.snr_med);
      return [`${str(p.mode) || "HF"} ${num(p.band_mhz) ?? "?"} MHz · ${num(p.n) ?? 0} spots on ${num(p.paths) ?? 0} paths`, snr !== null ? `median SNR ${snr} dB` : "", `path midpoints, ${t}`].filter(Boolean).join(" · ");
    }
    case "satnogs":
      return [`${str(p.sat_name) || `NORAD ${num(p.norad_cat_id) ?? "?"}`} ${str(p.rf_band)}`.trim(), `pass ${str(p.status)}`, t].join(" · ");
    case "sar_rfi": {
      const r = num(p.max_power_ratio);
      return [`${str(p.platform) || "Sentinel-1"} ${p.rfi_detected === true ? "RFI detected" : "no RFI flagged"}`, r !== null ? `interference strength ×${r.toFixed(1)}` : "", t].filter(Boolean).join(" · ");
    }
    case "smap": {
      const fr = num(p.rfi_frac);
      return [`SMAP L-band: ${num(p.n_rfi_h) ?? 0}H/${num(p.n_rfi_v) ?? 0}V of ${num(p.n_footprints) ?? 0} footprints flagged`, fr !== null ? `${Math.round(fr * 100)}%` : "", t].filter(Boolean).join(" · ");
    }
    case "gnss":
    case "cygnss":
    case "jammers":
    case "coverage":
      return "";
  }
}

/** Newest data time in a layer, for the "data for …" legend note on daily layers. */
export function newestLabel(features: LayerFeature[]): string {
  const ts = features.map((f) => Date.parse(f.properties.ts)).filter(Number.isFinite);
  if (!ts.length) return "no data yet";
  return `newest ${fmtDT(Math.max(...ts))}`;
}

/** HF spot radius (px) from spot count, log scale 3..14. */
export const hfRadius = (n: number | null): number => Math.min(14, Math.max(3, 3 + 3 * Math.log10((n ?? 1) + 1)));

// ------------------------------------------------------------------ status board

export type BandStateName = "no_coverage" | "learning" | "normal" | "elevated" | "anomalous";
export interface BandCell {
  state: BandStateName;
  value: number | null;
  median: number | null;
  z: number | null;
  n: number;
  n_history: number;
  explanations: string[];
  spark: (number | null)[];
}
export interface StatusBand {
  band: string;
  label: string;
  unit: string;
  direction: "high" | "low";
}
export interface StatusArea {
  aoi: string;
  label: string;
  bands: Record<string, BandCell>;
}
export interface SpectrumStatus {
  generated: string;
  bands: StatusBand[];
  areas: StatusArea[];
}

const STATES: BandStateName[] = ["no_coverage", "learning", "normal", "elevated", "anomalous"];
export const STATE_LABEL: Record<BandStateName, string> = {
  no_coverage: "No coverage",
  learning: "Learning",
  normal: "Normal",
  elevated: "Elevated",
  anomalous: "Anomalous",
};
/** Plain explanation of each state, for tooltips and the legend. */
export const STATE_HELP: Record<BandStateName, string> = {
  no_coverage: "Too few receivers here to judge. Not the same as quiet.",
  learning: "Collecting history; no judgement until the baseline is long enough.",
  normal: "Within the usual range for this area.",
  elevated: "Unusual (2 or more spreads from typical). Keep an eye on it.",
  anomalous: "Far from typical (3 or more spreads). Investigate.",
};
const RANK: Record<BandStateName, number> = { no_coverage: 0, learning: 1, normal: 2, elevated: 3, anomalous: 4 };

export function parseStatus(raw: unknown): SpectrumStatus | null {
  if (!isObj(raw) || !Array.isArray(raw.areas) || !Array.isArray(raw.bands)) return null;
  const bands: StatusBand[] = raw.bands.filter(isObj).map((b) => ({
    band: str(b.band),
    label: str(b.label),
    unit: str(b.unit),
    direction: b.direction === "low" ? "low" : "high",
  }));
  const areas: StatusArea[] = raw.areas.filter(isObj).map((a) => {
    const cells: Record<string, BandCell> = {};
    const src = isObj(a.bands) ? a.bands : {};
    for (const [k, v] of Object.entries(src)) {
      if (!isObj(v)) continue;
      const state = STATES.includes(v.state as BandStateName) ? (v.state as BandStateName) : "no_coverage";
      cells[k] = {
        state,
        value: num(v.value),
        median: num(v.median),
        z: num(v.z),
        n: num(v.n) ?? 0,
        n_history: num(v.n_history) ?? 0,
        explanations: Array.isArray(v.explanations) ? v.explanations.filter((x): x is string => typeof x === "string") : [],
        spark: Array.isArray(v.spark) ? v.spark.map((x) => num(x)) : [],
      };
    }
    return { aoi: str(a.aoi), label: str(a.label) || str(a.aoi), bands: cells };
  });
  return { generated: str(raw.generated), bands, areas };
}

/** Worst state of one band across all areas (drives the band-ladder marker). */
export function worstState(s: SpectrumStatus | null, band: string): BandStateName {
  let w: BandStateName = "no_coverage";
  for (const a of s?.areas ?? []) {
    const c = a.bands[band];
    if (c && RANK[c.state] > RANK[w]) w = c.state;
  }
  return w;
}

export function fmtValue(v: number | null, unit: string): string {
  if (v === null) return "–";
  const r = Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10;
  return unit === "%" ? `${r}%` : `${r}`;
}

/** One-sentence reading of a cell, e.g. "38 ships heard (1 h) vs typical 52 · 3.4 spreads towards trouble". */
export function cellSentence(c: BandCell, b: StatusBand): string {
  if (c.state === "no_coverage") return `${b.label}: ${STATE_HELP.no_coverage}`;
  const now = `${fmtValue(c.value, b.unit)} ${b.unit === "%" ? "" : b.unit} ${b.label}`.replace(/\s+/g, " ").trim();
  if (c.state === "learning") return `${now}. ${STATE_HELP.learning}`;
  const typ = c.median !== null ? ` vs typical ${fmtValue(c.median, b.unit)}` : "";
  const z = c.z !== null ? ` · ${Math.abs(c.z).toFixed(1)} spreads ${c.z >= 0 ? "towards trouble" : "on the safe side"}` : "";
  return `${now}${typ}${z}`;
}

/** SVG path (0..w x 0..h) for a sparkline; gaps (null) break the line. */
export function sparkPath(vals: (number | null)[], w = 60, h = 16): string {
  const xs = vals.map((v, i) => [i, v] as const).filter((p): p is readonly [number, number] => p[1] !== null);
  if (!xs.length) return "";
  const lo = Math.min(...xs.map((p) => p[1]));
  const hi = Math.max(...xs.map((p) => p[1]));
  const span = hi - lo || 1;
  const step = vals.length > 1 ? w / (vals.length - 1) : 0;
  let d = "";
  let prev = -2;
  for (const [i, v] of xs) {
    const x = (i * step).toFixed(1);
    const y = (h - ((v - lo) / span) * (h - 2) - 1).toFixed(1);
    d += `${i === prev + 1 ? "L" : "M"}${x} ${y} `;
    prev = i;
  }
  return d.trim();
}

/** What an analyst checks next, per band (the investigate checklist). */
export const NEXT_STEPS: Record<string, string[]> = {
  l1_gnss: [
    "Open the GNSS interference map for today and compare with the last few days",
    "Look for an estimated jammer location and official GNSS notices in this area",
    "Check whether ships nearby also report position jumps",
  ],
  l_1090: ["Check the aircraft feeds in Data sources: a drop is usually a receiver or feed outage, not jamming"],
  vhf_marine: ["Check AIS in Data sources; a drop across the whole area is usually reception, not ships going dark"],
  hf: [
    "Check space weather: a flare or geomagnetic storm explains most HF blackouts",
    "Compare neighbouring areas: a natural disturbance is wide, a local one is not",
    "Treat what is left as an unexplained HF anomaly; never name an emitter from this alone",
  ],
  sat_downlink: ["Look at which satellites failed: one satellite failing everywhere is the satellite, not interference"],
  l_1400: ["Compare with the last 30 days: a cell that is always flagged is a known emitter; a new one is the finding"],
  c_sar: ["Open the Sentinel-1 footprint and check the next pass to confirm the emitter is still active"],
};

export function jammerHover(p: Record<string, unknown>): string {
  const agr = num(p.agreement);
  return [
    `Estimated jammer ${str(p.ref)}`.trim(),
    str(p.method) === "horizon_fit" ? "fit from aircraft altitudes" : "centre of affected aircraft",
    agr !== null ? `${Math.round(agr * 100)}% of aircraft consistent` : "",
    `about ${Math.round(num(p.major_km) ?? 0)} km across`,
  ]
    .filter(Boolean)
    .join(" · ");
}
