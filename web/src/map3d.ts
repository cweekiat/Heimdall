// 3D view helpers: view mode, altitude mapping, drop lines, throttled lazy layer updates.
// Kept free of maplibre/deck imports so it can be unit tested with plain fakes.

export const OFFLINE_STYLE = "/basemap/style.json";
export const FT_TO_M = 0.3048;
export const TILT_3D = 60;

export type ViewMode = "2d" | "3d";

/** The minimal map surface the helpers need (maplibre-gl Map satisfies it). */
export interface MapLike {
  easeTo(o: { pitch?: number; bearing?: number; duration?: number }): unknown;
  setMaxPitch?(p: number): unknown;
  dragRotate?: { enable(): void; disable(): void };
  touchZoomRotate?: { enableRotation(): void; disableRotation(): void };
}

export function offlineStyleUrl(theme: "dark" | "light"): string {
  return `${OFFLINE_STYLE}?theme=${theme}`;
}

/** Make root-relative sprite/glyph URLs absolute (workers cannot resolve relative ones). */
export function absolutizeStyle<T extends { sprite?: unknown; glyphs?: unknown }>(style: T, origin: string): T {
  const fix = (v: unknown): unknown => (typeof v === "string" && v.startsWith("/") ? origin + v : v);
  const out = { ...style };
  if (style.sprite !== undefined) out.sprite = fix(style.sprite);
  if (style.glyphs !== undefined) out.glyphs = fix(style.glyphs);
  return out;
}

export function setViewMode(map: MapLike, mode: ViewMode): void {
  if (mode === "3d") {
    map.setMaxPitch?.(85);
    map.dragRotate?.enable();
    map.touchZoomRotate?.enableRotation();
    map.easeTo({ pitch: TILT_3D, duration: 400 });
  } else {
    map.easeTo({ pitch: 0, bearing: 0, duration: 400 });
  }
}

/** Altitude in metres for drawing. Ground, null and non-positive values draw at 0. */
export function altitudeMeters(altFt: number | null | undefined, onGround: boolean | undefined, exaggeration: number): number {
  if (onGround || altFt === null || altFt === undefined || !Number.isFinite(altFt) || altFt <= 0) return 0;
  return altFt * FT_TO_M * exaggeration;
}

export interface AirPos {
  lon: number;
  lat: number;
  alt_baro: number | null;
  on_ground?: boolean;
}

export function dropLines(rows: AirPos[], enabled: boolean, exaggeration: number): [number, number, number][][] {
  if (!enabled) return [];
  const out: [number, number, number][][] = [];
  for (const r of rows) {
    const z = altitudeMeters(r.alt_baro, r.on_ground, exaggeration);
    if (z > 0) out.push([[r.lon, r.lat, 0], [r.lon, r.lat, z]]);
  }
  return out;
}

export interface Clock {
  now(): number;
  /** Runs cb on the next frame (requestAnimationFrame in the browser). */
  frame(cb: () => void): void;
  later(cb: () => void, ms: number): void;
}

export const browserClock: Clock = {
  now: () => Date.now(),
  frame: (cb) => {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => cb());
    else setTimeout(cb, 16);
  },
  later: (cb, ms) => void setTimeout(cb, ms),
};

/** Coalesces bursts: runs `fn` at most `maxHz` times a second, on a frame, once per burst. */
export class Throttle {
  private last = -Infinity;
  private pending = false;
  private dirty = false;
  private fn: () => void;
  private maxHz: number;
  private clock: Clock;
  constructor(fn: () => void, maxHz = 2, clock: Clock = browserClock) {
    this.fn = fn;
    this.maxHz = maxHz;
    this.clock = clock;
  }

  poke(): void {
    this.dirty = true;
    if (this.pending) return;
    this.pending = true;
    const wait = Math.max(0, this.last + 1000 / this.maxHz - this.clock.now());
    const go = (): void => this.clock.frame(() => this.fire());
    if (wait > 0) this.clock.later(go, wait);
    else go();
  }

  private fire(): void {
    this.pending = false;
    if (!this.dirty) return;
    this.dirty = false;
    this.last = this.clock.now();
    this.fn();
  }
}

/** Layers are added on first enable only; hidden layers ignore updates; updates are throttled. */
export class LayerHub<D> {
  private on = new Set<string>();
  private added = new Set<string>();
  private latest = new Map<string, D>();
  private throttles = new Map<string, Throttle>();
  private add: (id: string) => void;
  private setData: (id: string, data: D) => void;
  private clock: Clock;
  private maxHz: number;
  constructor(add: (id: string) => void, setData: (id: string, data: D) => void, clock: Clock = browserClock, maxHz = 2) {
    this.add = add;
    this.setData = setData;
    this.clock = clock;
    this.maxHz = maxHz;
  }

  isAdded(id: string): boolean {
    return this.added.has(id);
  }

  isEnabled(id: string): boolean {
    return this.on.has(id);
  }

  setEnabled(id: string, enabled: boolean): void {
    if (!enabled) {
      this.on.delete(id);
      return;
    }
    this.on.add(id);
    if (!this.added.has(id)) {
      this.added.add(id);
      this.add(id);
    }
    if (this.latest.has(id)) this.throttle(id).poke();
  }

  update(id: string, data: D): void {
    if (!this.on.has(id)) return;
    this.latest.set(id, data);
    this.throttle(id).poke();
  }

  private throttle(id: string): Throttle {
    let t = this.throttles.get(id);
    if (!t) {
      t = new Throttle(
        () => {
          if (this.on.has(id)) this.setData(id, this.latest.get(id) as D);
        },
        this.maxHz,
        this.clock,
      );
      this.throttles.set(id, t);
    }
    return t;
  }
}

/** Outcome of the basemap availability probe; never throws. */
export async function probeBasemap(
  fetcher: (u: string) => Promise<{ ok: boolean; json(): Promise<unknown> }>,
): Promise<{ available: boolean; hint: string }> {
  try {
    const r = await fetcher("/api/basemap/status");
    const j = (await r.json()) as { available?: boolean; hint?: string };
    return { available: r.ok && j.available === true, hint: j.hint ?? "" };
  } catch {
    return { available: false, hint: "The offline map could not be checked." };
  }
}

interface ElLike {
  className: string;
  textContent: string | null;
  setAttribute(k: string, v: string): void;
}

/** Non-blocking hint shown when the offline map files are missing. */
export function unavailableHint(hint: string, doc: { createElement(t: string): ElLike }): ElLike {
  const el = doc.createElement("div");
  el.className = "map-hint";
  el.setAttribute("role", "status");
  el.textContent = `Offline map not installed. ${hint} Showing a plain background.`.replace(/\s+/g, " ").trim();
  return el;
}

/** Shared 3D view settings (read by the traffic layers each render). */
export const view = { mode: "2d" as ViewMode, exaggeration: 3, drop: false };
