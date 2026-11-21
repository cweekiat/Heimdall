// Plain-words explanation of an alert: its reference, the sources behind it and one sentence built from
// the footprint and evidence. Used by the map tooltip, the queue card and the inspector.
// Imports carry ".ts" so the offline tests can run this file directly under node.
import { fmtHM, tzLabel } from "./tz.ts";
import type { Alert, Evidence } from "./api.ts";
import { h } from "./dom.ts";
import { kindLabel, SOURCE_LABEL, TRUTH_LABEL } from "./labels.ts";

// "Things that could explain this" and "Can this be checked?" live in context.ts.
export { renderContext as contextBlock } from "./context.ts";

/** An area cell at the detection resolution is about 52 km across (edge to edge). */
export const AREA_KM = 52;

const hhmm = (iso: string): string => fmtHM(iso);

/** What happened, per alert type, in plain words. */
const WHAT: Record<string, string> = {
  gnss_jamming: "GPS jamming detected",
  gnss_spoofing: "GPS spoofing suspected",
  mil_orbit: "military aircraft circling",
  ais_gap: "ships stopped reporting",
  ais_spoofing: "ships reported impossible positions",
  thermal: "unusual heat detected",
  event_surge: "surge in reported events",
  warning_area: "activity inside a warning area",
};
const TRACK_WHAT: Record<string, string> = {
  ais_spoofing: "reported impossible positions",
  ais_gap: "stopped reporting its position",
  gnss_spoofing: "reported a false GPS position",
  mil_orbit: "was circling",
  gnss_jamming: "lost GPS",
};

export interface SourceRow {
  connector: string;
  family: string;
  grade: string;
  words: string;
}
export interface Explained {
  ref: string;
  sources: SourceRow[];
  summary: string;
}

function affected(ev: Evidence[]): number | null {
  const e = ev[0];
  const rule = e?.rules.find((r) => r.baseline !== null && r.value !== null && r.value <= 1);
  if (!e || !rule || rule.value === null) return null;
  const total = e.coverage.n_quality || e.coverage.n_observed;
  return total > 0 ? Math.round(rule.value * total) : null;
}

const isShip = (kind: string): boolean => kind.startsWith("ais_");

export function plainSummary(a: Alert, ev: Evidence[] = []): string {
  const from = ev.length ? ev.map((e) => e.window_start).sort()[0]! : a.first_seen;
  const to = ev.length ? ev.map((e) => e.window_end).sort().at(-1)! : a.last_seen;
  const when = `between ${hhmm(from)}–${hhmm(to)} ${tzLabel()}`;
  const fp = a.footprint;
  if (fp?.kind === "single_track") {
    const id = fp.description.replace(/^Track\s+/i, "");
    const noun = isShip(a.kind) ? "Ship" : "Aircraft";
    return `${noun} ${id} ${TRACK_WHAT[a.kind] ?? `flagged: ${kindLabel(a.kind).toLowerCase()}`} ${when}`;
  }
  const n = a.kind === "gnss_jamming" ? affected(ev) : null;
  const what = WHAT[a.kind] ?? `${kindLabel(a.kind).toLowerCase()} detected`;
  const tail = n !== null ? `, ${n} aircraft affected` : "";
  return `Area ~${AREA_KM} km where ${what} ${when}${tail}`;
}

export function explain(a: Alert, ev: Evidence[] = []): Explained {
  return {
    ref: a.ref ?? "no reference yet",
    sources: (a.sources ?? []).map((s) => {
      const g = `${s.admiralty.reliability}${s.admiralty.credibility}`;
      return {
        connector: s.connector,
        family: s.family,
        grade: g,
        words: `${SOURCE_LABEL[s.admiralty.reliability] ?? "Unknown"}, ${TRUTH_LABEL[s.admiralty.credibility] ?? "Unconfirmed"}`,
      };
    }),
    summary: plainSummary(a, ev),
  };
}

/** "adsb.lol, adsb.fi (ADS-B family)" style list of the connectors behind an alert. */
export function sourceNames(a: Alert): string {
  const xs = (a.sources ?? []).map((s) => s.connector.replace("_", "."));
  return xs.length ? [...new Set(xs)].join(", ") : "not recorded";
}

/** Compact block used in tooltips and cards: reference, sentence, sources by name. */
export function explainBlock(a: Alert, ev: Evidence[] = []): HTMLElement {
  const x = explain(a, ev);
  return h(
    "div",
    { class: "explain" },
    h("div", { class: "ref" }, x.ref),
    h("div", { class: "plain" }, x.summary),
    h("div", { class: "note" }, `Sources: ${sourceNames(a)} · confidence ${Math.round(a.confidence * 100)} %`),
  );
}
