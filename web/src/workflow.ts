// Alert workflow (task 042). One definition of the operational stages, the per-type checks an analyst runs,
// the false-alarm reasons and the "what to do next" text, shared by the alert queue and the alert details.
//
//   Needs action (new)  ->  Acknowledge: "I own this"
//   In progress (acknowledged / under review / shelved)  ->  check, then Confirm real | False alarm | Shelve
//   Confirmed (real, being tracked)  ->  Close when the activity has ended
//   Closed (resolved / false alarm / expired)  ->  history; Reopen if it comes back
import type { Alert, AlertStatus } from "./api";

export type Stage = "action" | "progress" | "confirmed" | "closed";

export const STAGES: { id: Stage; label: string; hint: string }[] = [
  { id: "action", label: "Needs action", hint: "New alerts nobody has taken yet" },
  { id: "progress", label: "In progress", hint: "Acknowledged, being checked, or shelved" },
  { id: "confirmed", label: "Confirmed", hint: "Real activity, still being tracked" },
  { id: "closed", label: "Closed", hint: "Resolved, false alarms and alerts that ended on their own" },
];

export function stageOf(s: AlertStatus): Stage {
  if (s === "new") return "action";
  if (s === "acknowledged" || s === "under_review" || s === "shelved") return "progress";
  if (s === "confirmed") return "confirmed";
  return "closed";
}

/** Minutes without fresh evidence after which the activity counts as ended (pipeline expiry is 60 min). */
export const ENDED_AFTER_MIN = 60;

/** Has the activity behind the alert stopped (no fresh evidence)? */
export function ended(a: Pick<Alert, "last_seen">, now = Date.now()): boolean {
  return now - new Date(a.last_seen).getTime() > ENDED_AFTER_MIN * 60_000;
}

/** Reasons offered when closing as a false alarm (stored with the alert and used to tune detectors). */
export const FALSE_ALARM_REASONS = [
  "Equipment fault on one aircraft or ship",
  "Data glitch or feed problem",
  "Known exercise, test or notice",
  "Duplicate of another alert",
  "Other",
];

export const SHELVE_HOURS = [2, 8, 24];

/** What the analyst checks before deciding, per alert type. Plain words, no jargon. */
export const CHECKS: Record<string, string[]> = {
  gnss_jamming: [
    "Several different airlines or aircraft types affected, not just one fleet",
    "It lasts more than one 5-minute step and is not one aircraft passing through",
    "Daily GPS interference map shows the same area",
    "No geomagnetic storm in the space-weather chip (a storm can explain it)",
    "Any notice (NOTAM / navigation warning) or ship position problems nearby",
  ],
  gnss_spoofing: [
    "Open the aircraft's track: the position jumps away and stays wrong",
    "The aircraft still reports good GPS accuracy while wrong",
    "Other aircraft nearby show the same thing",
    "Known spoofing area, or jamming nearby",
  ],
  ais_spoofing: [
    "Ship positions jump, overlap or circle in a way ships cannot move",
    "More than one ship affected",
    "GPS jamming or spoofing reported for aircraft nearby",
  ],
  ais_gap: [
    "The ship was in good receiver coverage when it went silent",
    "No port call or anchorage explains the silence",
    "It reappeared somewhere unexpected",
  ],
  mil_orbit: ["Aircraft type and operator", "Orbit near jamming, a notice or a border", "Repeats at the same place"],
  mil_deployment: [
    "Aircraft types and operators flowing in",
    "Where they landed or stayed",
    "Other build-up indicators in the same theatre (tankers, transport, surveillance)",
  ],
  mil_surge: ["Which roles are above normal", "Exercise or notice that explains it"],
  mil_high_interest: ["Which aircraft and where it is going", "Does it match other activity"],
  mil_buildup: ["Each indicator group listed under why it was flagged", "Exercise or notice that explains it"],
  thermal: ["Satellite image or news of a fire or strike", "Agricultural or industrial burning season"],
  spectrum_anomaly: ["Which band and how far from normal", "Space weather or propagation that explains it"],
};

/** The one-line instruction shown at the top of the alert details. */
export function nextStep(a: Pick<Alert, "status" | "last_seen">, now = Date.now()): string {
  switch (a.status) {
    case "new":
      return "Acknowledge to take ownership, then run the checks below.";
    case "acknowledged":
    case "under_review":
      return "Run the checks below, then Confirm if it is real or close it as a false alarm. Shelve it if you need to wait.";
    case "shelved":
      return "Shelved: it comes back to In progress when the shelve time ends.";
    case "confirmed":
      return ended(a, now)
        ? "The activity has stopped. Close the alert to record that it ended."
        : "Real and still active. Keep tracking it; close it once the activity stops.";
    case "resolved":
      return "Closed as real activity that has ended. Reopen if it starts again.";
    case "dismissed":
      return "Closed as a false alarm. Reopen if that was wrong.";
    default:
      return "Ended on its own: no fresh evidence. Nothing to do.";
  }
}
