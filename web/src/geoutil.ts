// Small geodesy helpers: MGRS, great-circle distance.

const A = 6378137;
const F = 1 / 298.257223563;
const K0 = 0.9996;

export function haversineNm(a: [number, number], b: [number, number]): number {
  const r = (d: number): number => (d * Math.PI) / 180;
  const dLat = r(b[1] - a[1]);
  const dLon = r(b[0] - a[0]);
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(r(a[1])) * Math.cos(r(b[1])) * Math.sin(dLon / 2) ** 2;
  return (2 * 6371.0088 * Math.asin(Math.sqrt(x))) / 1.852;
}

/** Lat/lon (deg) to MGRS at 1 m precision, e.g. "48P UT 12345 67890". Returns "-" outside 80S..84N. */
export function toMgrs(lat: number, lon: number): string {
  if (lat < -80 || lat > 84) return "-";
  const lonN = ((((lon + 180) % 360) + 360) % 360) - 180;
  const zone = Math.min(60, Math.floor((lonN + 180) / 6) + 1);
  const lon0 = ((zone - 1) * 6 - 180 + 3) * (Math.PI / 180);
  const phi = (lat * Math.PI) / 180;
  const lam = (lonN * Math.PI) / 180;
  const e2 = F * (2 - F);
  const ep2 = e2 / (1 - e2);
  const N = A / Math.sqrt(1 - e2 * Math.sin(phi) ** 2);
  const T = Math.tan(phi) ** 2;
  const C = ep2 * Math.cos(phi) ** 2;
  const Aa = Math.cos(phi) * (lam - lon0);
  const M =
    A *
    ((1 - e2 / 4 - (3 * e2 ** 2) / 64 - (5 * e2 ** 3) / 256) * phi -
      ((3 * e2) / 8 + (3 * e2 ** 2) / 32 + (45 * e2 ** 3) / 1024) * Math.sin(2 * phi) +
      ((15 * e2 ** 2) / 256 + (45 * e2 ** 3) / 1024) * Math.sin(4 * phi) -
      ((35 * e2 ** 3) / 3072) * Math.sin(6 * phi));
  const easting =
    K0 * N * (Aa + ((1 - T + C) * Aa ** 3) / 6 + ((5 - 18 * T + T ** 2 + 72 * C - 58 * ep2) * Aa ** 5) / 120) + 500000;
  let northing =
    K0 *
    (M + N * Math.tan(phi) * (Aa ** 2 / 2 + ((5 - T + 9 * C + 4 * C ** 2) * Aa ** 4) / 24 + ((61 - 58 * T + T ** 2 + 600 * C - 330 * ep2) * Aa ** 6) / 720));
  if (lat < 0) northing += 10000000;
  const bands = "CDEFGHJKLMNPQRSTUVWXX";
  const band = bands[Math.floor((lat + 80) / 8)]!;
  const setCols = ["ABCDEFGH", "JKLMNPQR", "STUVWXYZ"];
  const col = setCols[(zone - 1) % 3]![Math.floor(easting / 100000) - 1] ?? "?";
  const rowLetters = "ABCDEFGHJKLMNPQRSTUV";
  const rowIdx = (Math.floor(northing / 100000) + (zone % 2 === 0 ? 5 : 0)) % 20;
  const row = rowLetters[rowIdx]!;
  const pad = (v: number): string => String(Math.floor(v % 100000)).padStart(5, "0");
  return `${zone}${band} ${col}${row} ${pad(easting)} ${pad(northing)}`;
}

export const fmtLatLon = (lat: number, lon: number): string =>
  `${Math.abs(lat).toFixed(4)}°${lat >= 0 ? "N" : "S"} ${Math.abs(lon).toFixed(4)}°${lon >= 0 ? "E" : "W"}`;
