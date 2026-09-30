// Single glossary of user-facing words. Plain language only: no jargon on screen.
// Raw technical terms live in technical.ts (the "Technical details" disclosure) and nowhere else.
import type { Alert, Aoi } from "./api";
import type { IconName } from "./icons";

export const KIND_LABEL: Record<string, string> = {
  gnss_jamming: "GPS jamming",
  gnss_spoofing: "GPS spoofing (fake position)",
  mil_orbit: "Military aircraft circling",
  ais_spoofing: "Ship position faked",
  ais_gap: "Ship went dark",
  thermal: "Heat / fire detected",
  outage: "Internet or power outage",
  event_surge: "Burst of news events",
  warning_area: "Warning area",
  composite: "Several signs in one place",
};

export const KIND_ICON: Record<string, IconName> = {
  gnss_jamming: "radio",
  gnss_spoofing: "gps",
  mil_orbit: "plane",
  ais_spoofing: "ship",
  ais_gap: "ship",
  composite: "waypoints",
};

/** Source reliability (letter) in words. */
export const SOURCE_LABEL: Record<string, string> = {
  A: "Reliable",
  B: "Usually reliable",
  C: "Fairly reliable",
  D: "Not usually reliable",
  E: "Unreliable",
  F: "Cannot be judged",
};
/** Information credibility (digit) in words. */
export const TRUTH_LABEL: Record<number, string> = {
  1: "Confirmed",
  2: "Probably true",
  3: "Possibly true",
  4: "Doubtful",
  5: "Improbable",
  6: "Unconfirmed",
};
export const sourceLabel = (r: string): string => `Source: ${SOURCE_LABEL[r] ?? "Unknown"}`;
export const truthLabel = (c: number): string => TRUTH_LABEL[c] ?? "Unconfirmed";

export const CORROBORATION_LABEL: Record<string, string> = {
  corroborated: "Backed by other sources",
  uncorroborated: "No second source yet",
  contradicted: "Other sources disagree",
};

export const confidenceLabel = (c: number): string => (c >= 0.75 ? "High" : c >= 0.45 ? "Medium" : "Low");
export const coverageLabel = (c: number): string => (c >= 0.7 ? "Good" : c >= 0.4 ? "Limited" : "Poor");

export const PRIORITY_WORD: Record<string, string> = { P1: "Critical", P2: "High", P3: "Medium", P4: "Low" };

export const T = {
  brand: "HEIMDALL",
  brandTip: "Public-source information only. Alerts are unconfirmed until a second source agrees.",
  area: "Area",
  areas: "Areas",
  alerts: "Alerts",
  type: "Type",
  status: "Status",
  source: "Source",
  layers: "Layers & filters",
  sources: "Data sources",
  actions: "Actions",
  timeline: "Timeline",
  shift: "Shift report",
  help: "Help",
  search: "Search alerts",
  theme: "Change colour theme",
  resetLayout: "Reset window layout",
  zoomIn: "Zoom in",
  zoomOut: "Zoom out",
  measure: "Measure distance",
  ring: "Range ring",
  coverageLayer: "Show where data is thin",
  live: "Live",
  offline: "Offline",
  reconnecting: "Reconnecting",
  technical: "Technical details",
  clearsWhen: "Clears when",
  noneToShow: "Nothing to show.",
  selectPrompt: "Select an alert from the list or map to see why it fired.",
  noAlerts: "No alerts match. No alerts does not mean nothing is happening.",
  noSecondSource: "No second source has reported this yet. That is not evidence it is false.",
  dataCoverage: (w: string): string => `Data coverage: ${w}`,
  seenFor: (min: number): string => `seen for ${min < 1 ? "<1" : Math.round(min)} min`,
  since: (t: string): string => `started ${t}`,
  affectedNow: (pct: string, normal: string): string => `${pct} of aircraft affected now. Normally ${normal}.`,
  lostGps: (n: number, of: number, pct: string, normal: string): string =>
    `${n} of ${of} aircraft (${pct}) lost GPS accuracy. Normally ${normal}.`,
  muchHigher: "much higher than normal",
  normalLevel: "normal level",
  ack: "Acknowledge",
  review: "Start review",
  confirm: "Confirm",
  dismiss: "Dismiss",
  shelve: "Snooze",
  note: "Add note",
  toastGoTo: "Go to",
  toastAck: "Acknowledge",
  placeholderSources: "Data sources appear here.",
  placeholderActions: "Actions appear here.",
} as const;

let aoiLabels: Record<string, string> = {};

export function setAois(aois: Aoi[]): void {
  aoiLabels = Object.fromEntries(aois.map((a) => [a.name, a.label ?? prettify(a.name)]));
}

const prettify = (s: string): string => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

export const kindLabel = (k: string): string => KIND_LABEL[k] ?? prettify(k);
export const aoiName = (n: string | null): string => (n ? (aoiLabels[n] ?? prettify(n)) : "Unassigned");

/** Human title, e.g. "GPS jamming — South China Sea". */
export const alertTitle = (a: Alert): string => `${kindLabel(a.kind)} — ${aoiName(a.aoi)}`;
