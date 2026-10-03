// Aircraft investigation helpers (task 047): track colouring by altitude (IntelSky-style key) and short text.
// Pure: no DOM. Ramp avoids red (alerts) and cyan (satellites).
import type { Investigation, TypeReference } from "./api.ts";

export const ALT_STOPS: [number, [number, number, number]][] = [
  [0, [255, 196, 60]],
  [10000, [130, 210, 90]],
  [20000, [70, 150, 255]],
  [30000, [150, 110, 240]],
  [40000, [230, 110, 220]],
];
const GROUND: [number, number, number] = [140, 150, 160];

export function altColor(alt: number, alpha = 1): [number, number, number, number] {
  const a = Math.round(alpha * 255);
  if (alt < 0) return [...GROUND, a];
  const top = ALT_STOPS[ALT_STOPS.length - 1]!;
  if (alt >= top[0]) return [...top[1], a];
  for (let i = 0; i < ALT_STOPS.length - 1; i++) {
    const [a0, c0] = ALT_STOPS[i]!;
    const [a1, c1] = ALT_STOPS[i + 1]!;
    if (alt <= a1) {
      const f = (alt - a0) / (a1 - a0);
      return [0, 1, 2].map((k) => Math.round(c0[k]! + f * (c1[k]! - c0[k]!))).concat(a) as [number, number, number, number];
    }
  }
  return [...top[1], a];
}

export interface Segment {
  path: [number, number][];
  color: [number, number, number, number];
}

/** One coloured segment per pair of points; earlier flights dimmer; no line across a data gap (> 90 min). */
export function trackSegments(inv: Pick<Investigation, "track" | "flight_from">): Segment[] {
  const out: Segment[] = [];
  const t = inv.track;
  for (let i = 1; i < t.length; i++) {
    const a = t[i - 1]!;
    const b = t[i]!;
    if (b[3] - a[3] > 90 * 60) continue;
    const alt = b[2] >= 0 ? b[2] : a[2];
    out.push({ path: [[a[0], a[1]], [b[0], b[1]]], color: altColor(alt, i > inv.flight_from ? 0.95 : 0.35) });
  }
  return out;
}

export function refLines(r: TypeReference | null): [string, string][] {
  if (!r) return [];
  const rows: [string, string | null][] = [
    ["Built for", r.mission],
    ["Range", r.range_km ? `about ${r.range_km.toLocaleString("en")} km` : null],
    ["Endurance", r.endurance_h ? `about ${r.endurance_h} h` : null],
    ["Crew", r.crew !== null && r.crew !== undefined ? (r.crew === 0 ? "uncrewed" : String(r.crew)) : null],
    ["Refuelling", r.refuelling],
    ["Can do", r.capabilities],
  ];
  return rows.filter((x): x is [string, string] => !!x[1]);
}

export const CONF_WORD: Record<string, string> = { likely: "Likely", possible: "Possible" };
