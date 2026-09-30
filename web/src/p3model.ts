// P3 layer model (task 023b): pure functions, no deck.gl or DOM, so they run under node --test.
// Wire shapes follow docs/tasks/023a-context-api.md. Every string here ends up in textContent.

export type P3Layer = "thermal" | "outage" | "night_lights" | "imagery" | "overpass";
export const P3_LAYERS: P3Layer[] = ["thermal", "outage", "night_lights", "imagery", "overpass"];
export const P3_LABEL: Record<P3Layer, string> = {
  thermal: "Heat spots (satellite)",
  outage: "Internet outages",
  night_lights: "Night lights",
  imagery: "Satellite image footprints",
  overpass: "Next satellite passes",
};
/** Refresh period per layer, in ms. Night lights change once a day. */
export const P3_REFRESH_MS: Record<P3Layer, number> = {
  thermal: 300_000,
  outage: 300_000,
  night_lights: 1_800_000,
  imagery: 300_000,
  overpass: 300_000,
};
/** Only heat spots are on until the analyst turns the rest on. */
export const P3_DEFAULT_ON: P3Layer[] = ["thermal"];

export interface LayerProps {
  kind: string;
  source: string;
  ts: string;
  valid_from: string | null;
  valid_to: string | null;
  value: number | null;
  [k: string]: unknown;
}
export interface LayerFeature {
  type: "Feature";
  id: string;
  geometry: { type: string; coordinates: unknown } | null;
  properties: LayerProps;
}
export interface LayerFC {
  type: "FeatureCollection";
  features: LayerFeature[];
  truncated: boolean;
}

export const TRUNCATED_NOTICE = "Too many items to show. Zoom in or shorten the time window.";

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** Accepts anything the API returns and keeps only well-formed features. */
export function parseLayer(raw: unknown): LayerFC {
  const feats: LayerFeature[] = [];
  const list = isObj(raw) && Array.isArray(raw.features) ? raw.features : [];
  for (const f of list) {
    if (!isObj(f) || !isObj(f.properties)) continue;
    const g = isObj(f.geometry) && typeof f.geometry.type === "string" ? (f.geometry as LayerFeature["geometry"]) : null;
    feats.push({ type: "Feature", id: String(f.id ?? ""), geometry: g, properties: f.properties as LayerProps });
  }
  return { type: "FeatureCollection", features: feats, truncated: isObj(raw) && raw.truncated === true };
}

export const fmtZ = (iso: string | null | undefined): string =>
  iso ? `${new Date(iso).toISOString().slice(0, 16).replace("T", " ")}Z` : "unknown time";

/** Heat-spot radius in pixels from fire radiative power (MW), log scale, 4..18 px. */
export function thermalRadius(frp: number | null): number {
  const v = frp !== null && frp > 0 ? frp : 1;
  return Math.min(18, Math.max(4, 4 + 4.5 * Math.log10(v + 1)));
}

export function pointOf(f: LayerFeature): [number, number] | null {
  const g = f.geometry;
  if (!g) return null;
  if (g.type === "Point" && Array.isArray(g.coordinates)) return [Number(g.coordinates[0]), Number(g.coordinates[1])];
  if (g.type === "Polygon" && Array.isArray(g.coordinates)) {
    const ring = (g.coordinates as number[][][])[0] ?? [];
    if (!ring.length) return null;
    return [ring.reduce((s, p) => s + p[0]!, 0) / ring.length, ring.reduce((s, p) => s + p[1]!, 0) / ring.length];
  }
  return null;
}

export function ringOf(f: LayerFeature): number[][] | null {
  const g = f.geometry;
  if (g?.type !== "Polygon" || !Array.isArray(g.coordinates)) return null;
  return (g.coordinates as number[][][])[0] ?? null;
}

/** Break a closed ring into short pieces with gaps: a dashed outline without extra deck extensions. */
export function dashSegments(ring: number[][], on = 2, off = 1): number[][][] {
  const out: number[][][] = [];
  const pts = ring.slice(0, -1);
  const n = pts.length;
  const steps = 8;
  let i = 0;
  for (let e = 0; e < n; e++) {
    const a = pts[e]!;
    const b = pts[(e + 1) % n]!;
    for (let s = 0; s < steps; s++) {
      const at = (k: number): number[] => [a[0]! + ((b[0]! - a[0]!) * k) / steps, a[1]! + ((b[1]! - a[1]!) * k) / steps];
      if (i % (on + off) < on) out.push([at(s), at(s + 1)]);
      i++;
    }
  }
  return out;
}

export const isRadar = (p: LayerProps): boolean => str(p.platform).toLowerCase().startsWith("sentinel-1");

/** Night lights: brightness compared with the usual, as a multiple (1 = usual). Null if unknown. */
export function lightsVsUsual(p: LayerProps): number | null {
  const med = num(p.median);
  const v = num(p.value);
  return med !== null && med > 0 && v !== null ? v / med : null;
}

/** Days between the observation date and now. Night-lights imagery always lags. */
export function imageryLagDays(p: LayerProps, now = Date.now()): number | null {
  const t = Date.parse(str(p.date) || p.ts);
  return Number.isFinite(t) ? Math.max(0, Math.round((now - t) / 86_400_000)) : null;
}

export function hoverText(layer: P3Layer, f: LayerFeature, now = Date.now()): string {
  const p = f.properties;
  switch (layer) {
    case "thermal": {
      const frp = num(p.value);
      return [`Heat spot · ${str(p.satellite) || "satellite"}`, fmtZ(p.ts), frp !== null ? `${frp} MW` : "power unknown", `confidence ${str(p.confidence) || "unknown"}`].join(" · ");
    }
    case "outage": {
      const who = str(p.entity_code) || str(p.location) || "unknown place";
      const obs = num(p.observed);
      const base = num(p.baseline);
      const share = obs !== null && base ? `${Math.round((obs / base) * 100)}% of normal` : str(p.outage_type) || "outage reported";
      return [`Internet outage · ${who}`, share, fmtZ(p.ts)].join(" · ");
    }
    case "night_lights": {
      const r = lightsVsUsual(p);
      const lag = imageryLagDays(p, now);
      return [
        r !== null ? `Night lights at ${Math.round(r * 100)}% of the 30-night median` : `Night lights ${num(p.value) ?? "unknown"} nW/cm²/sr`,
        lag !== null ? `imagery lag ${lag} days` : "imagery lag unknown",
      ].join(" · ");
    }
    case "imagery": {
      const cc = num(p.cloud_cover);
      return [str(p.platform) || "Satellite image", fmtZ(p.valid_from ?? p.ts), cc !== null ? `cloud ${Math.round(cc)}%` : "cloud n/a (radar)"].join(" · ");
    }
    case "overpass":
      return [`${str(p.sat_name) || "Satellite"} ${str(p.sensor)}`.trim(), fmtZ(p.valid_from ?? p.ts)].join(" · ");
  }
}

export interface PassChip {
  aoi: string;
  text: string;
}
/** "next pass" chip per area: the soonest pass at or after `now`. */
export function nextPass(aoi: string, fc: LayerFC, now = Date.now()): PassChip | null {
  const soon = fc.features
    .map((f) => ({ f, t: Date.parse(f.properties.valid_from ?? f.properties.ts) }))
    .filter((x) => Number.isFinite(x.t) && x.t >= now)
    .sort((a, b) => a.t - b.t)[0];
  return soon ? { aoi, text: `next pass: ${hoverText("overpass", soon.f)}` } : null;
}
