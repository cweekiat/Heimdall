// Display timezone (Settings > Time zone). Default Singapore time. Data, APIs and the GNSS daily map
// stay in UTC; only what the analyst reads is converted.
export const DEFAULT_TZ = "Asia/Singapore";
const KEY = "hm.tz";
/** Short names for common zones (Intl often only gives "GMT+8"). */
const ABBR: Record<string, string> = { UTC: "UTC", "Asia/Singapore": "SGT", "Asia/Kuala_Lumpur": "MYT", "Asia/Bangkok": "ICT", "Asia/Jakarta": "WIB", "Asia/Manila": "PHT", "Asia/Tokyo": "JST", "Asia/Hong_Kong": "HKT", "Asia/Taipei": "CST", "Asia/Dubai": "GST", "Europe/London": "UK", "Australia/Sydney": "AET" };
/** Short list shown first in Settings. */
export const COMMON_TZ = ["Asia/Singapore", "UTC", "Asia/Kuala_Lumpur", "Asia/Jakarta", "Asia/Bangkok", "Asia/Manila", "Asia/Hong_Kong", "Asia/Taipei", "Asia/Tokyo", "Asia/Kolkata", "Asia/Dubai", "Europe/London", "Europe/Berlin", "America/New_York", "Australia/Sydney"];

export function validTz(t: string): boolean {
  try {
    new Intl.DateTimeFormat("en-GB", { timeZone: t });
    return true;
  } catch {
    return false;
  }
}

function load(): string {
  try {
    const t = globalThis.localStorage?.getItem(KEY);
    if (t && validTz(t)) return t;
  } catch {
    /* no storage */
  }
  return DEFAULT_TZ;
}

let tz = load();
const listeners: (() => void)[] = [];
export const getTz = (): string => tz;
export const onTz = (fn: () => void): void => void listeners.push(fn);

export function setTz(t: string): void {
  if (!validTz(t) || t === tz) return;
  tz = t;
  try {
    globalThis.localStorage?.setItem(KEY, t);
  } catch {
    /* private mode */
  }
  listeners.forEach((f) => f());
}

/** UTC offset of the zone at `at`, e.g. "UTC+8", "UTC-3:30". */
export function tzOffset(zone: string = tz, at = Date.now()): string {
  const p = new Intl.DateTimeFormat("en-GB", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(at);
  const g = (k: string): number => Number(p.find((x) => x.type === k)?.value);
  const asUtc = Date.UTC(g("year"), g("month") - 1, g("day"), g("hour"), g("minute"));
  const min = Math.round((asUtc - Math.floor(at / 60000) * 60000) / 60000);
  if (min === 0) return "UTC";
  const s = min > 0 ? "+" : "-";
  const a = Math.abs(min);
  return `UTC${s}${Math.floor(a / 60)}${a % 60 ? `:${String(a % 60).padStart(2, "0")}` : ""}`;
}

/** "SGT", "UTC" or the offset for zones without a common short name. */
export const tzLabel = (zone: string = tz): string => ABBR[zone] ?? tzOffset(zone);

function parts(t: number, zone: string): Record<string, string> {
  const p = new Intl.DateTimeFormat("en-GB", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(t);
  return Object.fromEntries(p.map((x) => [x.type, x.value]));
}

const toMs = (v: string | number | Date): number => (typeof v === "number" ? v : new Date(v).getTime());

/** "10-01 14:32 SGT" (month-day, 24 h). */
export function fmtDT(v: string | number | Date, zone: string = tz): string {
  const p = parts(toMs(v), zone);
  return `${p.month}-${p.day} ${p.hour}:${p.minute} ${tzLabel(zone)}`;
}

/** "2026-10-01 14:32:05 SGT" (clock). */
export function fmtClock(v: string | number | Date = Date.now(), zone: string = tz): string {
  const p = parts(toMs(v), zone);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} ${tzLabel(zone)}`;
}

/** "14:32" in the display zone (labels that state the zone elsewhere). */
export function fmtHM(v: string | number | Date, zone: string = tz): string {
  const p = parts(toMs(v), zone);
  return `${p.hour}:${p.minute}`;
}
