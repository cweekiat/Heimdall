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
  drawAreas: "Draw areas",
  alerts: "Alerts",
  type: "Type",
  status: "Status",
  source: "Source",
  layers: "Layers & filters",
  view3d: "Switch between flat (2D) and tilted (3D) map",
  compass: "Reset north and flatten the map",
  sources: "Data sources",
  actions: "Actions",
  timeline: "Timeline",
  shift: "Shift report",
  help: "Help",
  about: "About",
  clearAll: "Clear all alerts",
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
  shipClass: { cargo: "Cargo", tanker: "Tanker", passenger: "Passenger", fishing: "Fishing", military: "Military", other: "Other" } as Record<string, string>,
  searchTraffic: "Callsign, ship name or MMSI",
  mapExtras: "Map extras",
  clearFilters: "Clear filters",
  dataOnMap: "Show on map",
  aircraft: "Aircraft",
  ships: "Ships",
  ship: "Ship",
  alertMarkers: "Alerts",
  noticesLayer: "Warnings and NOTAMs",
  sourcesToShow: "Data sources",
  quickFilters: "Quick filters",
  militaryOnly: "Military only",
  onlyInAlerts: "Only things in alerts",
  altitudeBand: "Altitude",
  altLow: "Lowest altitude (feet)",
  altHigh: "Highest altitude (feet)",
  shipTypes: "Ship type",
  noSources: "No sources listed.",
  onGround: "On the ground",
  unknown: "Unknown",
  yes: "Yes",
  notInView: "Not in view right now.",
  gpsOk: "GPS OK",
  gpsProblem: "GPS problem",
  notInAlert: "This one is not part of an alert",
  showRelated: "Show related alert",
  noRelated: "No open alert found",
  follow: "Follow",
  callsign: "Callsign",
  registration: "Registration",
  aircraftType: "Aircraft type",
  altitude: "Altitude",
  speed: "Speed",
  heading: "Heading",
  squawk: "Transponder code",
  gps: "GPS",
  reportedBy: "Reported by",
  lastHeard: "Last heard",
  military: "Military",
  shipName: "Name",
  shipType: "Type",
  course: "Course",
  destination: "Destination",
  underway: "Under way",
  stationary: "Stationary",
  trailAir: "Path shown on the map: last 30 min",
  trailSea: "Path shown on the map: last 2 h",
  noTrail: "no recent positions",
  closeCard: "Close",
  notice: "Notice",
  noticeNotam: "Airspace notice (NOTAM)",
  noticeWarning: "Sea warning",
  reference: "Reference",
  noticeType: "Type",
  validFrom: "Valid from",
  validTo: "Valid until",
  originalText: "Original text (unverified)",
  stopCollecting: (name: string): string => `Stop collecting from ${name}?`,
  stop: "Stop",
  keep: "Keep on",
  on: "On",
  off: "Off",
  refresh: "Refresh",
  sourcesNote: "Old data lowers alert confidence. A quiet source is a gap in what we can see, not a quiet region.",
  colProposed: "Proposed",
  colApproved: "Approved",
  colDone: "Done",
  colRejected: "Rejected",
  highRisk: "High risk",
  approve: "Approve",
  reject: "Reject",
  cancel: "Cancel",
  confirmApprove: "Yes, approve",
  confirmHighRisk: (label: string): string => `${label} is high risk. Approve it?`,
  actionsNote: "Approving records the decision. Nothing runs automatically.",
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
