// Pure data logic for the live traffic layers (no DOM, no deck): padded boxes, merge by id,
// stale expiry, response decoding (GeoJSON, compact, delta) and the zoom cross-fade. Node-tested.

export type Box = [number, number, number, number]; // w, s, e, n (w > e: crosses the antimeridian)

export const parseBox = (s: string): Box => {
  const p = s.split(",").map(Number);
  return [p[0] ?? -180, p[1] ?? -90, p[2] ?? 180, p[3] ?? 90];
};
export const fmtBox = (b: Box): string => b.map((x) => x.toFixed(3)).join(",");
const widthOf = (b: Box): number => (b[0] <= b[2] ? b[2] - b[0] : b[2] + 360 - b[0]);
const wrap = (x: number): number => ((((x + 180) % 360) + 360) % 360) - 180;

/** Grow a box by `frac` of its size on every side (default 50%); world-wide boxes stay world-wide. */
export function padBox(b: Box, frac = 0.5): Box {
  const w = widthOf(b);
  const lat = (b[3] - b[1]) * frac;
  const s = Math.max(-90, b[1] - lat);
  const n = Math.min(90, b[3] + lat);
  if (w * (1 + 2 * frac) >= 360) return [-180, s, 180, n];
  return [wrap(b[0] - w * frac), s, wrap(b[2] + w * frac), n];
}

export function inBox(b: Box, lon: number, lat: number): boolean {
  if (lat < b[1] || lat > b[3]) return false;
  return b[0] <= b[2] ? lon >= b[0] && lon <= b[2] : lon >= b[0] || lon <= b[2];
}

/** Is `inner` fully inside `outer`? (axis-aligned boxes, antimeridian aware) */
export function boxInside(inner: Box, outer: Box): boolean {
  if (inner[1] < outer[1] || inner[3] > outer[3]) return false;
  if (outer[0] === -180 && outer[2] === 180) return true;
  const rel = (((inner[0] - outer[0]) % 360) + 360) % 360;
  return rel + widthOf(inner) <= widthOf(outer);
}

// ---- entity store: merge by id, keep still-fresh entities that fell outside the new response

export interface Entry<T> {
  row: T;
  seen: number;
}
type Pt = { lon: number; lat: number };

const same = (a: object, b: object): boolean => {
  const x = a as Record<string, unknown>;
  const y = b as Record<string, unknown>;
  const ka = Object.keys(x);
  if (ka.length !== Object.keys(y).length) return false;
  for (const k of ka) {
    const u = x[k];
    const v = y[k];
    if (u !== v && !(Array.isArray(u) && Array.isArray(v) && u.join() === v.join())) return false;
  }
  return true;
};

export class EntityStore<T extends Pt> {
  private m = new Map<string, Entry<T>>();
  private cache: T[] = [];
  /** Bumps only when the visible list really changed; consumers use it as a cache key. */
  version = 0;
  private key: (r: T) => string;
  private maxAgeMs: number;
  constructor(key: (r: T) => string, maxAgeMs: number) {
    this.key = key;
    this.maxAgeMs = maxAgeMs;
  }

  list(): T[] {
    return this.cache;
  }
  get size(): number {
    return this.m.size;
  }

  private upsert(r: T, now: number, into: Map<string, Entry<T>>): boolean {
    const k = this.key(r);
    const old = this.m.get(k);
    if (old && same(old.row, r)) {
      into.set(k, { row: old.row, seen: now });
      return false;
    }
    into.set(k, { row: r, seen: now });
    return true;
  }

  private commit(next: Map<string, Entry<T>>, changed: boolean): boolean {
    if (next.size !== this.m.size) changed = true;
    this.m = next;
    if (changed) {
      this.cache = [...next.values()].map((e) => e.row);
      this.version++;
    }
    return changed;
  }

  /** A full response for `req`: replaces what the server covered, keeps fresh rows that fell outside it
   *  but were inside the previous fetch box (so panning never blanks the map). */
  replaceFull(rows: T[], now: number, req: Box, prev: Box | null): boolean {
    const next = new Map<string, Entry<T>>();
    let changed = false;
    for (const r of rows) if (this.upsert(r, now, next)) changed = true;
    for (const [k, e] of this.m) {
      if (next.has(k)) continue;
      if (prev && !inBox(req, e.row.lon, e.row.lat) && inBox(prev, e.row.lon, e.row.lat) && now - e.seen <= this.maxAgeMs) next.set(k, e);
    }
    return this.commit(next, changed);
  }

  /** A delta: upsert changed rows, drop removed ids, expire what has not been refreshed. */
  applyDelta(changedRows: T[], removed: string[], now: number): boolean {
    const next = new Map(this.m);
    let changed = false;
    for (const r of changedRows) if (this.upsert(r, now, next)) changed = true;
    for (const id of removed) if (next.delete(id)) changed = true;
    for (const [k, e] of next) {
      if (now - e.seen > this.maxAgeMs) {
        next.delete(k);
        changed = true;
      }
    }
    return this.commit(next, changed);
  }

  /** Drop entries that have gone stale. */
  expire(now: number): boolean {
    return this.applyDelta([], [], now);
  }

  clear(): boolean {
    return this.commit(new Map(), this.m.size > 0);
  }
}

// ---- response decoding

export interface DensityRow {
  h3: string;
  count: number;
  mil: number;
}

/** Density cells to draw: with "military only", just the cells holding military, counted by military alone. */
export function densityCells(all: DensityRow[], milOnly: boolean): DensityRow[] {
  return milOnly ? all.filter((c) => c.mil > 0).map((c) => ({ ...c, count: c.mil })) : all;
}
export interface Decoded<T> {
  kind: "points" | "density";
  delta: boolean;
  rows: T[];
  removed: string[];
  cells: DensityRow[];
  version: number | null;
  truncated: boolean;
  total: number | null;
}

interface AnyResp {
  type?: string;
  features?: { geometry: { type: string; coordinates: unknown }; properties: Record<string, unknown> }[];
  cells?: DensityRow[];
  cols?: Record<string, unknown[]>;
  columns?: Record<string, unknown[]>;
  removed?: unknown[];
  delta?: boolean;
  version?: unknown;
  truncated?: unknown;
  total?: unknown;
}

/** Tolerant decoder: plain GeoJSON, density, and (when the server supports them) delta and compact
 *  (columnar arrays: `cols` with lon and lat plus one array per property). */
export function decode<T extends Pt>(resp: unknown, sinceAsked: boolean): Decoded<T> {
  const r = (resp ?? {}) as AnyResp;
  const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const base = {
    removed: Array.isArray(r.removed) ? r.removed.map(String) : [],
    version: num(r.version),
    truncated: r.truncated === true,
    total: num(r.total),
  };
  const delta = sinceAsked && (r.delta === true || Array.isArray(r.removed));
  if (r.type === "density") return { ...base, kind: "density", delta: false, rows: [], cells: r.cells ?? [] };
  const cols = r.cols ?? r.columns;
  let rows: T[] = [];
  if (cols && Array.isArray(cols.lon) && Array.isArray(cols.lat)) {
    const names = Object.keys(cols);
    const n = cols.lon.length;
    rows = Array.from({ length: n }, (_, i) => Object.fromEntries(names.map((k) => [k, (cols[k] as unknown[])[i]])) as unknown as T);
  } else if (r.features) {
    rows = r.features
      .filter((f) => f.geometry.type === "Point")
      .map((f) => {
        const [lon, lat] = f.geometry.coordinates as [number, number];
        return { ...f.properties, lon, lat } as unknown as T;
      });
  }
  return { ...base, kind: "points", delta, rows, cells: [] };
}

// ---- zoom cross-fade and mode hysteresis

export const FADE_LO = 4.5;
export const FADE_HI = 5.5;
export const HYST = 0.15;
const clamp01 = (x: number): number => Math.min(1, Math.max(0, x));
/** Opacity of the individual-icon layer: 0 at zoom 4.5 rising to 1 at 5.5. Density is the complement. */
export const iconOpacity = (zoom: number): number => clamp01((zoom - FADE_LO) / (FADE_HI - FADE_LO));
export const densityOpacity = (zoom: number): number => 1 - iconOpacity(zoom);

export interface Wanted {
  points: boolean;
  density: boolean;
}
/** Which datasets to hold. Both inside the fade band; each switches on/off with hysteresis so
 *  jittering around a threshold does not refetch or blank a layer. */
export function wanted(prev: Wanted, zoom: number): Wanted {
  const points = prev.points ? zoom >= FADE_LO - HYST : zoom >= FADE_LO;
  const density = prev.density ? zoom < FADE_HI + HYST : zoom < FADE_HI;
  return { points, density: density || !points };
}

/** `zoom` value to send: the server picks density below 5 and points from 5 up. */
export const zoomParam = (kind: "points" | "density", zoom: number): number => (kind === "points" ? Math.max(zoom, 5) : Math.min(zoom, 4.99));

export const truncationNote = (shown: number, total: number | null): string =>
  total !== null && total > shown
    ? `Showing ${shown.toLocaleString("en-US")} of ${total.toLocaleString("en-US")}; zoom in to see the rest.`
    : `Showing the first ${shown.toLocaleString("en-US")}; zoom in to see the rest.`;

export const COVERAGE_NOTE =
  "Coverage: ships come from terrestrial AIS receivers only, so open ocean is often empty. Worldwide aircraft need OpenSky keys; without them you see only what the free ADS-B feeds cover.";
