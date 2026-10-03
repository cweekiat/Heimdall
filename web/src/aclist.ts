// Aircraft list logic (task 046, IntelSky-style): text filters, role filter, sort, in-view test and exports.
// Pure: no DOM. Community strings (operators, names) are only ever written as text or escaped XML.
import type { MilAircraft } from "./api.ts";
import { ROLE_LABEL, ROLE_ORDER } from "./milroles.ts";

export type SortKey = "ident" | "type" | "operator" | "country" | "role" | "alt" | "gs";
export type Tab = "all" | "view" | "selected";

export interface ListQuery {
  callsign: string;
  type: string; // type code or type name
  hex: string;
  operator: string; // operator or country
  roles: string[]; // empty = all
}

export const EMPTY_QUERY: ListQuery = { callsign: "", type: "", hex: "", operator: "", roles: [] };

const has = (v: string | null | undefined, q: string): boolean => !q || (v ?? "").toLowerCase().includes(q.trim().toLowerCase());

export function matchQuery(a: MilAircraft, q: ListQuery): boolean {
  return (
    has(a.flight ?? a.registration, q.callsign) &&
    (has(a.type, q.type) || has(a.type_name, q.type)) &&
    has(a.hex, q.hex) &&
    (has(a.operator, q.operator) || has(a.country, q.operator)) &&
    (q.roles.length === 0 || q.roles.includes(a.role))
  );
}

export const ident = (a: MilAircraft): string => (a.flight || a.registration || a.hex.toUpperCase()).trim();

/** "w,s,e,n" (w > e crosses the antimeridian) contains the aircraft. */
export function inBbox(a: { lat: number; lon: number }, bbox: string): boolean {
  const [w, s, e, n] = bbox.split(",").map(Number) as [number, number, number, number];
  if (a.lat < s || a.lat > n) return false;
  return w <= e ? a.lon >= w && a.lon <= e : a.lon >= w || a.lon <= e;
}

const val = (a: MilAircraft, k: SortKey): string | number => {
  switch (k) {
    case "ident":
      return ident(a).toLowerCase();
    case "type":
      return (a.type ?? "~").toLowerCase();
    case "operator":
      return (a.operator ?? "~").toLowerCase();
    case "country":
      return (a.country ?? "~").toLowerCase();
    case "role":
      return ROLE_ORDER.indexOf(a.role) < 0 ? 99 : ROLE_ORDER.indexOf(a.role);
    case "alt":
      return a.on_ground ? 0 : (a.alt ?? -1);
    case "gs":
      return a.gs ?? -1;
  }
};

export function sortRows(rows: MilAircraft[], key: SortKey, desc: boolean): MilAircraft[] {
  const dir = desc ? -1 : 1;
  return [...rows].sort((x, y) => {
    const a = val(x, key);
    const b = val(y, key);
    return a < b ? -dir : a > b ? dir : x.hex < y.hex ? -1 : 1;
  });
}

export function selectRows(all: MilAircraft[], tab: Tab, q: ListQuery, bbox: string, selected: ReadonlySet<string>): MilAircraft[] {
  return all.filter((a) => matchQuery(a, q) && (tab === "all" || (tab === "view" ? inBbox(a, bbox) : selected.has(a.hex))));
}

const csvCell = (v: unknown): string => {
  const s = v === null || v === undefined ? "" : String(v);
  // quote everything; neutralise spreadsheet formulas from community text
  return `"${(/^[=+\-@]/.test(s) ? `'${s}` : s).replace(/"/g, '""')}"`;
};

export function toCsv(rows: MilAircraft[]): string {
  const head = ["hex", "callsign", "registration", "type", "type_name", "role", "operator", "country", "lat", "lon", "alt_ft", "speed_kt", "track", "seen_utc"];
  const lines = rows.map((a) =>
    [a.hex, a.flight, a.registration, a.type, a.type_name, ROLE_LABEL[a.role] ?? a.role, a.operator, a.country, a.lat, a.lon, a.on_ground ? 0 : a.alt, a.gs, a.track, a.ts].map(csvCell).join(","),
  );
  return [head.join(","), ...lines].join("\n");
}

const xml = (s: string): string => s.replace(/[<>&'"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", "'": "&apos;", '"': "&quot;" })[c]!);

export function toKml(rows: MilAircraft[], title = "Heimdall military aircraft"): string {
  const marks = rows.map((a) => {
    const desc = [a.type_name || a.type, a.operator || a.country, ROLE_LABEL[a.role] ?? a.role, a.on_ground ? "on ground" : a.alt !== null ? `${a.alt} ft` : ""]
      .filter(Boolean)
      .join(" · ");
    const altM = a.on_ground || a.alt === null ? 0 : Math.round(a.alt * 0.3048);
    return `<Placemark><name>${xml(ident(a))}</name><description>${xml(desc)}</description><TimeStamp><when>${xml(a.ts)}</when></TimeStamp><Point><altitudeMode>absolute</altitudeMode><coordinates>${a.lon},${a.lat},${altM}</coordinates></Point></Placemark>`;
  });
  return `<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>${xml(title)}</name>${marks.join("")}</Document></kml>\n`;
}
