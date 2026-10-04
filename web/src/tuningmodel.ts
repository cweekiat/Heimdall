// Detector tuning model (task 061): parse /api/tuning and phrase precision. Pure, runs under node --test.

export interface TuningProposal {
  detector: string;
  rule: string;
  config: string;
  current: number;
  proposed: number;
  false_removed: number;
  false_total: number;
  real_kept: number;
  why: string;
  yaml: string;
}
export interface TuningDetector {
  detector: string;
  alerts: number;
  reviewed: number;
  real: number;
  false: number;
  unreviewed: number;
  precision: number | null;
  precision_ci95: [number, number] | null;
  reasons: Record<string, number>;
  proposals: TuningProposal[];
  notes: string[];
  hints: string[];
}
export interface TuningReport {
  generated: string;
  days: number;
  alerts: number;
  reviewed: number;
  detectors: TuningDetector[];
  rules: { min_false: number; min_real: number; min_gain: number };
  limits: string;
}

/** Plain names for detector ids. */
export const DETECTOR_NAME: Record<string, string> = {
  jamming: "GPS jamming (aircraft)",
  spoofing: "GPS spoofing (aircraft)",
  orbit: "Military orbits",
  ais_spoofing: "Ship position spoofing",
  ais_gap: "Ships going dark",
  military: "Military air activity",
  cygnss: "GPS noise from orbit (CYGNSS)",
  hf_path: "HF propagation (reflection point)",
  hf_rx: "HF reception (receivers)",
  spectrum_state: "Band indicators",
  thermal: "Heat spots",
  warning_area: "Warning areas",
};

export function precisionText(d: Pick<TuningDetector, "precision" | "precision_ci95" | "reviewed">): string {
  if (d.precision === null || !d.reviewed) return "no reviews yet";
  const ci = d.precision_ci95;
  const range = ci ? ` (likely ${Math.round(ci[0] * 100)}–${Math.round(ci[1] * 100)} %)` : "";
  return `${Math.round(d.precision * 100)} % real of ${d.reviewed} reviewed${range}`;
}

/** Reviews still needed before proposals appear, or "" when enough. */
export function needText(d: Pick<TuningDetector, "real" | "false">, r: TuningReport["rules"]): string {
  const f = Math.max(0, r.min_false - d.false);
  const c = Math.max(0, r.min_real - d.real);
  if (!f && !c) return "";
  return [f ? `${f} more false alarm${f > 1 ? "s" : ""}` : "", c ? `${c} more confirmed` : ""].filter(Boolean).join(" and ") + " needed for threshold proposals";
}
