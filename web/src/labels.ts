import type { Alert, Aoi } from "./api";

export const KIND_LABEL: Record<string, string> = {
  gnss_jamming: "GNSS jamming",
  gnss_spoofing: "GNSS spoofing",
  mil_orbit: "Military orbit",
  ais_spoofing: "AIS spoofing",
  ais_gap: "AIS gap",
  thermal: "Thermal anomaly",
  outage: "Outage",
  event_surge: "Event surge",
  warning_area: "Warning area",
  composite: "Composite",
};

let aoiLabels: Record<string, string> = {};

export function setAois(aois: Aoi[]): void {
  aoiLabels = Object.fromEntries(aois.map((a) => [a.name, a.label ?? prettify(a.name)]));
}

const prettify = (s: string): string => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

export const kindLabel = (k: string): string => KIND_LABEL[k] ?? prettify(k);
export const aoiName = (n: string | null): string => (n ? (aoiLabels[n] ?? prettify(n)) : "Unassigned");

/** Human title, e.g. "GNSS jamming - South China Sea". */
export const alertTitle = (a: Alert): string => `${kindLabel(a.kind)} — ${aoiName(a.aoi)}`;
