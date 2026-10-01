// Satellites around the Earth (God's-Eye style): live position (SGP4 on the server from CelesTrak
// elements, interpolated here every second along the predicted track) drawn AT ALTITUDE, with a drop
// line to the ground point and the past / next track. On the flat map (no tilt) altitude is not
// visible; on the globe it is. Heights above 2,000 km are compressed (GNSS orbits at ~20,000 km
// would otherwise sit far off-screen); the hover card always gives the true altitude.
import { LineLayer, PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { api } from "./api";
import type { SatPos } from "./api";
import { ago, h } from "./dom";
import { filters, onFilters } from "./filters";
import type { OpsMap } from "./map";
import { rgba, satRgba as sat, surfaceRgba } from "./theme/palette";

const POLL_MS = 60_000;
const STEP_S = 60;
type P3 = [number, number, number];

/** Display height in metres: true below 2,000 km, compressed 10:1 above. */
export function displayAltM(km: number): number {
  const k = Math.max(0, km);
  return (k <= 2000 ? k : 2000 + (k - 2000) * 0.1) * 1000;
}

/** Position `sec` seconds after the track start, interpolated (handles the antimeridian). */
export function along(track: P3[] | [number, number][], sec: number): P3 | null {
  if (!track.length) return null;
  const t = track as number[][];
  const at = (i: number): P3 => [t[i]![0]!, t[i]![1]!, t[i]![2] ?? 0];
  const i = Math.floor(sec / STEP_S);
  if (i < 0) return at(0);
  if (i >= t.length - 1) return at(t.length - 1);
  const f = (sec - i * STEP_S) / STEP_S;
  const [x0, y0, z0] = at(i);
  let [x1, y1, z1] = at(i + 1);
  if (x1 - x0 > 180) x1 -= 360;
  if (x0 - x1 > 180) x1 += 360;
  let x = x0 + (x1 - x0) * f;
  if (x > 180) x -= 360;
  if (x < -180) x += 360;
  return [x, y0 + (y1 - y0) * f, z0 + (z1 - z0) * f];
}

/** Split a track where it crosses the antimeridian so 2D paths do not streak across the map. */
export function splitDateline<T extends number[]>(track: T[]): T[][] {
  const out: T[][] = [];
  let cur: T[] = [];
  for (const p of track) {
    const prev = cur[cur.length - 1];
    if (prev && Math.abs(p[0]! - prev[0]!) > 180) {
      if (cur.length > 1) out.push(cur);
      cur = [];
    }
    cur.push(p);
  }
  if (cur.length > 1) out.push(cur);
  return out;
}

interface Live {
  sat: SatPos;
  pos: P3; // lon, lat, display metres
  km: number;
}

const lifted = (p: number[]): P3 => [p[0]!, p[1]!, displayAltM(p[2] ?? 0)];
/** GNSS constellations: many satellites, so dots only (no tracks, no labels). */
const busy = (s: SatPos): boolean => s.group === "GNSS";

export class Satellites {
  private sats: SatPos[] = [];
  private fetchedAt = 0;
  private tip: HTMLElement;
  private timers: number[] = [];

  constructor(
    private ops: OpsMap,
    wrap: HTMLElement,
  ) {
    this.tip = h("div", { class: "hovercard tip", hidden: true });
    wrap.append(this.tip);
    onFilters(() => {
      if (filters.satellites && !this.sats.length) void this.refresh();
      this.ops.renderSoon();
    });
  }

  onLoad: () => void = () => undefined;
  /** Satellites currently tracked and shown (for the filter page). */
  count = (): number => this.shown().length;

  start(): void {
    void this.refresh();
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && Date.now() - this.fetchedAt > POLL_MS) void this.refresh();
    });
    this.timers.push(window.setInterval(() => void this.refresh(), POLL_MS));
    this.timers.push(window.setInterval(() => filters.satellites && this.sats.length && this.ops.renderSoon(), 1000));
  }

  async refresh(): Promise<void> {
    if (!filters.satellites || document.visibilityState === "hidden") return;
    try {
      const r = await api.satellites();
      this.sats = r.satellites;
      this.fetchedAt = Date.now();
      this.ops.renderSoon();
      this.onLoad();
    } catch {
      /* keep the last picture */
    }
  }

  private shown(): SatPos[] {
    return this.sats.filter((s) => !filters.hiddenSatGroups.includes(s.group));
  }

  private live(list: SatPos[]): Live[] {
    const sec = (Date.now() - this.fetchedAt) / 1000;
    return list.map((sat) => {
      const p = along(sat.next, sec) ?? [sat.lon, sat.lat, sat.alt_km];
      return { sat, km: p[2], pos: [p[0], p[1], displayAltM(p[2])] };
    });
  }

  layers(): unknown[] {
    if (!filters.satellites || !this.sats.length) return [];
    const list = this.shown();
    const live = this.live(list);
    const tracked = list.filter((s) => !busy(s));
    const past = tracked.flatMap((s) => splitDateline(s.past.map(lifted)).map((p) => ({ p })));
    const next = tracked.flatMap((s) => splitDateline(s.next.map(lifted)).map((p) => ({ p })));
    const tick = Math.floor(Date.now() / 1000);
    return [
      new PathLayer<{ p: P3[] }>({ id: "sat-past", data: past, getPath: (d) => d.p, getColor: sat(0.3), widthMinPixels: 1.5 }),
      new PathLayer<{ p: P3[] }>({ id: "sat-next", data: next, getPath: (d) => d.p, getColor: sat(0.75), widthMinPixels: 1.5 }),
      new LineLayer<Live>({
        id: "sat-drop",
        data: live.filter((d) => !busy(d.sat)),
        getSourcePosition: (d) => [d.pos[0], d.pos[1], 0],
        getTargetPosition: (d) => d.pos,
        getColor: sat(0.35),
        getWidth: 1,
        updateTriggers: { getSourcePosition: tick, getTargetPosition: tick },
      }),
      new ScatterplotLayer<Live>({
        id: "sat-ground",
        data: live.filter((d) => !busy(d.sat)),
        getPosition: (d) => [d.pos[0], d.pos[1], 0],
        getRadius: 2.5,
        radiusUnits: "pixels",
        getFillColor: sat(0.5),
        updateTriggers: { getPosition: tick },
      }),
      new ScatterplotLayer<Live>({
        id: "sat-pos",
        data: live,
        getPosition: (d) => d.pos,
        getRadius: (d) => (busy(d.sat) ? 3.5 : 6),
        radiusUnits: "pixels",
        getFillColor: (d) => (busy(d.sat) ? sat(0.75) : sat(1)),
        getLineColor: rgba("base", 1),
        stroked: true,
        getLineWidth: (d) => (busy(d.sat) ? 0.5 : 2),
        lineWidthUnits: "pixels",
        pickable: true,
        updateTriggers: { getPosition: tick },
        onHover: (info) => this.hover(info.object as Live | undefined, info.x, info.y),
      }),
      new TextLayer<Live>({
        id: "sat-name",
        data: live.filter((d) => !busy(d.sat)),
        getPosition: (d) => d.pos,
        getText: (d) => d.sat.name,
        getSize: 11,
        getColor: rgba("base", 0.95),
        getPixelOffset: [0, -14],
        fontWeight: 600,
        outlineWidth: 2,
        outlineColor: surfaceRgba(0.9),
        fontSettings: { sdf: true },
        updateTriggers: { getPosition: tick },
      }),
    ];
  }

  private hover(d: Live | undefined, x: number, y: number): void {
    if (!d) {
      this.tip.hidden = true;
      return;
    }
    this.tip.hidden = false;
    this.tip.style.left = `${x + 12}px`;
    this.tip.style.top = `${y + 12}px`;
    const scale = d.km > 2000 ? " (height drawn compressed)" : "";
    this.tip.textContent = `${d.sat.name}${d.sat.sensor ? ` (${d.sat.sensor})` : ""} · ${d.sat.group} · ${Math.round(d.km).toLocaleString()} km up${scale} · elements from ${ago(d.sat.epoch)}`;
  }
}
