// Pure geometry and validation helpers for drawing areas of operation (task 015). No DOM, no map.
// Limits mirror the server (heimdall/schemas.py): the server is the authority, this only saves a round trip.

export type LonLat = [number, number];
export type LatLon = [number, number];

export const NAME_MAX = 40;
export const NAME_RE = /^[A-Za-z0-9_-][A-Za-z0-9 _-]{0,39}$/;
export const RADIUS_MIN_NM = 5;
export const RADIUS_MAX_NM = 500;
export const VERTEX_MIN = 3;
export const VERTEX_MAX = 200;
export const SPAN_MAX_DEG = 10;
export const MAX_USER_AREAS = 20;

const KM_PER_NM = 1.852;
const R_KM = 6371.0088;
const rad = (d: number): number => (d * Math.PI) / 180;
const deg = (r: number): number => (r * 180) / Math.PI;

/** Great-circle distance between two [lon, lat] points, nautical miles. */
export function distNm(a: LonLat, b: LonLat): number {
  const x = Math.sin(rad(b[1] - a[1]) / 2) ** 2 + Math.cos(rad(a[1])) * Math.cos(rad(b[1])) * Math.sin(rad(b[0] - a[0]) / 2) ** 2;
  return (2 * R_KM * Math.asin(Math.min(1, Math.sqrt(x)))) / KM_PER_NM;
}

/** Closed [lon, lat] ring approximating a circle (n segments). */
export function circleRing(lat: number, lon: number, nm: number, n = 90): LonLat[] {
  const r = (nm * KM_PER_NM) / R_KM;
  const la = rad(lat);
  const lo = rad(lon);
  const out: LonLat[] = [];
  for (let i = 0; i <= n; i++) {
    const b = (i / n) * 2 * Math.PI;
    const p = Math.asin(Math.sin(la) * Math.cos(r) + Math.cos(la) * Math.sin(r) * Math.cos(b));
    const l = lo + Math.atan2(Math.sin(b) * Math.sin(r) * Math.cos(la), Math.cos(r) - Math.sin(la) * Math.sin(p));
    out.push([deg(l), deg(p)]);
  }
  return out;
}

/** Closed [lon, lat] ring for a polygon given as server-style [lat, lon] vertices. */
export function polygonRing(poly: LatLon[]): LonLat[] {
  const r = poly.map((p): LonLat => [p[1], p[0]]);
  if (r.length) r.push(r[0]!);
  return r;
}

/** [lat, lon] vertices from map [lon, lat] clicks, rounded to 5 decimals (about 1 m). */
export const toLatLon = (pts: LonLat[]): LatLon[] => pts.map((p) => [Math.round(p[1] * 1e5) / 1e5, Math.round(p[0] * 1e5) / 1e5]);

/** Largest latitude and longitude extent of the vertices, degrees. */
export function span(poly: LatLon[]): { lat: number; lon: number } {
  const la = poly.map((p) => p[0]);
  const lo = poly.map((p) => p[1]);
  return { lat: Math.max(...la) - Math.min(...la), lon: Math.max(...lo) - Math.min(...lo) };
}

/** Twice the signed area (shoelace) of [lat, lon] vertices; 0 means a line or a point. */
export function area2(poly: LatLon[]): number {
  let a = 0;
  poly.forEach((p, i) => {
    const q = poly[(i + 1) % poly.length]!;
    a += p[1] * q[0] - q[1] * p[0];
  });
  return a;
}

/** Ray-casting point-in-polygon; point and vertices are [lat, lon]. */
export function pointInPolygon(pt: LatLon, poly: LatLon[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [yi, xi] = poly[i]!;
    const [yj, xj] = poly[j]!;
    if (yi > pt[0] !== yj > pt[0] && pt[1] < ((xj - xi) * (pt[0] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

const okLat = (v: number): boolean => Number.isFinite(v) && v >= -90 && v <= 90;
const okLon = (v: number): boolean => Number.isFinite(v) && v >= -180 && v <= 180;

/** Plain-words problem with a name, or null when it is acceptable. */
export function nameProblem(name: string): string | null {
  if (!name.trim()) return "Give the area a name.";
  if (name.length > NAME_MAX) return `Use at most ${NAME_MAX} characters.`;
  if (!NAME_RE.test(name)) return "Use only letters, numbers, spaces, dashes and underscores.";
  return null;
}

export function circleProblem(lat: number, lon: number, nm: number): string | null {
  if (!okLat(lat) || !okLon(lon)) return "The centre is outside the map.";
  if (!Number.isFinite(nm) || nm < RADIUS_MIN_NM) return `The radius must be at least ${RADIUS_MIN_NM} nautical miles.`;
  if (nm > RADIUS_MAX_NM) return `The radius must be at most ${RADIUS_MAX_NM} nautical miles.`;
  return null;
}

export function polygonProblem(poly: LatLon[]): string | null {
  if (poly.length < VERTEX_MIN) return `Click at least ${VERTEX_MIN} points.`;
  if (poly.length > VERTEX_MAX) return `Use at most ${VERTEX_MAX} points.`;
  if (!poly.every((p) => okLat(p[0]) && okLon(p[1]))) return "A point is outside the map.";
  const s = span(poly);
  if (s.lat > SPAN_MAX_DEG || s.lon > SPAN_MAX_DEG) return `The area is too large: at most ${SPAN_MAX_DEG} degrees across.`;
  if (Math.abs(area2(poly)) < 1e-9) return "The points are in a line. Spread them out.";
  return null;
}

/** Drops a repeated last point (a double-click adds the same point twice). */
export function dedupeTail(pts: LonLat[], epsilon = 1e-9): LonLat[] {
  const out = [...pts];
  while (out.length > 1 && Math.abs(out[out.length - 1]![0] - out[out.length - 2]![0]) < epsilon && Math.abs(out[out.length - 1]![1] - out[out.length - 2]![1]) < epsilon) out.pop();
  return out;
}

/** One outline the map draws: a config area, or a user area (active or not). */
export interface DisplayAo {
  id: string;
  ring: LonLat[];
  role: string;
  origin: "config" | "user";
  active: boolean;
  selected: boolean;
}
