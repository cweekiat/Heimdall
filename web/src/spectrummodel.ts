// Spectrum model (task 030, ADR-008): bands, layer groups, GNSS map colours, hover text.
// Pure functions, no deck.gl or DOM, so they run under node --test. All strings end up in textContent.
import type { LayerFeature } from "./p3model.ts";

// ------------------------------------------------------------------ bands (ladder, frequency order)

export type Band = "hf" | "vhf_marine" | "l_1090" | "l1_gnss" | "l_1400" | "c_sar";
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
  { band: "l_1090", name: "L 1090", range: "1090 MHz", sources: ["adsb_lol", "adsb_fi", "opensky_global"] },
  { band: "l1_gnss", name: "L1 GNSS", range: "1559–1610 MHz", sources: ["adsb_lol", "adsb_fi", "aisstream", "faa_nms_notam", "nga_msi"] },
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

/** Band filter: alerts carry `bands` (task 028); older alerts without it never match a band. */
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

export type SpectrumLayer = "gnss" | "hf" | "satnogs" | "sar_rfi" | "smap";
export const SPECTRUM_LAYERS: SpectrumLayer[] = ["hf", "satnogs", "gnss", "sar_rfi", "smap"];
export const LIVE_LAYERS: SpectrumLayer[] = ["hf", "satnogs"];
export const DAILY_LAYERS: SpectrumLayer[] = ["gnss", "sar_rfi", "smap"];
export const SPECTRUM_LABEL: Record<SpectrumLayer, string> = {
  hf: "HF propagation spots (WSPR, PSK Reporter)",
  satnogs: "Satellite passes (SatNOGS ground stations)",
  gnss: "GNSS interference (daily, GPSJam method)",
  sar_rfi: "C-band interference (Sentinel-1)",
  smap: "L-band interference (SMAP)",
};
/** Hours of data requested per layer. */
export const SPECTRUM_HOURS: Record<Exclude<SpectrumLayer, "gnss">, number> = { hf: 1, satnogs: 6, sar_rfi: 72, smap: 48 };
export const SPECTRUM_REFRESH_MS: Record<SpectrumLayer, number> = { hf: 120_000, satnogs: 300_000, gnss: 300_000, sar_rfi: 600_000, smap: 600_000 };
export const SPECTRUM_DEFAULT_ON: SpectrumLayer[] = ["gnss"];

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

// ------------------------------------------------------------------ hover text for event layers

export function spectrumHover(layer: SpectrumLayer, f: LayerFeature): string {
  const p = f.properties;
  const t = p.ts ? `${new Date(p.ts).toISOString().slice(0, 16).replace("T", " ")}Z` : "";
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
      return "";
  }
}

/** Newest data time in a layer, for the "data for …" legend note on daily layers. */
export function newestLabel(features: LayerFeature[]): string {
  const ts = features.map((f) => Date.parse(f.properties.ts)).filter(Number.isFinite);
  if (!ts.length) return "no data yet";
  return `newest ${new Date(Math.max(...ts)).toISOString().slice(0, 16).replace("T", " ")}Z`;
}

/** HF spot radius (px) from spot count, log scale 3..14. */
export const hfRadius = (n: number | null): number => Math.min(14, Math.max(3, 3 + 3 * Math.log10((n ?? 1) + 1)));
