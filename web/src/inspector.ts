// Alert details: one short, factual view. What was detected, where and when, which sources saw it,
// why it was flagged (each rule: measured value against its threshold and the normal level) and the
// affected aircraft / vessels. 042: a "Next step" box with the checks for this alert type, and actions that
// follow the workflow (Acknowledge -> Confirm real / False alarm (reason) / Shelve -> Close: ended / Reopen).
// No grading adjectives.
import type { AlertDetail, AlertStatus, Evidence, Rule } from "./api";
import { fmtNum, fmtTime, h } from "./dom";
import { fmtLatLon, toMgrs } from "./geoutil";
import { icon, iconButton } from "./icons";
import { aoiName, kindLabel, T } from "./labels";
import { CHECKS, FALSE_ALARM_REASONS, nextStep, SHELVE_HOURS, stageOf } from "./workflow";
import { PRIORITY, STATUS_LABEL, table } from "./panels";
import { patch, state, triage } from "./store";

/** Reason recorded when an alert is closed as a false alarm from the keyboard (d). */
export const CLEAR_REASON = "False alarm (closed from the keyboard)";

let error = "";

const pct = (v: number): string => `${v < 0.1 ? (v * 100).toFixed(1) : Math.round(v * 100)} %`;
const OP: Record<string, string> = { ">=": "≥", "<=": "≤", ">": ">", "<": "<", "==": "=", "!=": "≠" };

/** Source key / provider -> what kind of data it is. */
const SOURCE_TYPE: Record<string, string> = {
  "adsb.lol": "ADS-B",
  "adsb.fi": "ADS-B",
  adsb_lol: "ADS-B",
  adsb_fi: "ADS-B",
  opensky: "ADS-B",
  aisstream: "AIS",
  nga_msi: "NGA navigation warnings",
  faa_nms_notam: "FAA NOTAMs",
  firms: "NASA FIRMS heat detections",
  swpc: "NOAA space weather",
  open_meteo: "Open-Meteo weather",
  celestrak: "CelesTrak orbits",
  wspr_live: "WSPR",
  pskreporter: "PSK Reporter",
  satnogs: "SatNOGS",
  sentinel1_rfi: "Sentinel-1 RFI",
  smap_rfi: "SMAP RFI",
};
const DOMAIN_NAME: Record<string, string> = { air: "Aircraft (ADS-B)", sea: "Ships (AIS)", space: "Satellite", events: "Weather / notices", spectrum: "Radio spectrum", cyber: "Network" };

/** "ADS-B (adsb.lol, adsb.fi)" from the providers and record sources of the evidence. */
export function sourceLine(evs: Evidence[]): string {
  const by = new Map<string, Set<string>>();
  for (const e of evs) {
    for (const p of [...e.coverage.providers, ...e.records.map((r) => r.source)]) {
      const t = SOURCE_TYPE[p] ?? p;
      if (!by.has(t)) by.set(t, new Set());
      if (t !== p) by.get(t)!.add(p.replace("_", "."));
    }
  }
  return [...by.entries()].map(([t, ps]) => (ps.size ? `${t} (${[...ps].sort().join(", ")})` : t)).join("; ") || "Not recorded";
}

/** One factual line per rule: "<what>: <value> (alert at ≥ <threshold>; normal <baseline>)". */
export function ruleLine(r: Rule): string {
  const nums = [r.value, r.threshold, r.baseline].filter((x): x is number => x !== null);
  const share = /share|ratio|fraction|percent|%/i.test(r.description) && nums.every((x) => x >= 0 && x <= 1);
  const f = (v: number): string => (share ? pct(v) : fmtNum(v, 1));
  const ctx = [r.threshold !== null ? `alert at ${OP[r.op] ?? r.op} ${f(r.threshold)}` : "", r.baseline !== null ? `normal ${f(r.baseline)}` : ""].filter(Boolean).join("; ");
  return `${r.description}: ${r.value === null ? "n/a" : f(r.value)}${ctx ? ` (${ctx})` : ""}`;
}

/** Entity id -> kind and label: 6 hex chars = aircraft (ICAO), 9 digits = vessel (MMSI). */
export function entityLabel(e: string): { kind: "aircraft" | "vessel" | "other"; text: string } {
  if (/^[0-9a-f]{6}$/i.test(e)) return { kind: "aircraft", text: `Aircraft ${e.toUpperCase()}` };
  if (/^\d{9}$/.test(e)) return { kind: "vessel", text: `Vessel MMSI ${e}` };
  return { kind: "other", text: e };
}

const kv = (rows: [string, Node | string | null | false][]): HTMLElement =>
  h("dl", { class: "facts-kv" }, ...rows.filter(([, v]) => v !== null && v !== false && v !== "").flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v as Node | string)]));

function body(d: AlertDetail): Node[] {
  const a = d.alert;
  const evs = d.evidence;
  const ev = evs[0];
  const mins = Math.max(0, Math.round((new Date(a.last_seen).getTime() - new Date(a.first_seen).getTime()) / 60000));
  const dur = mins >= 120 ? `${(mins / 60).toFixed(1)} h` : `${mins} min`;
  const f = ev?.features ?? {};
  const affected = typeof f.bad === "number" && typeof f.total === "number" ? `${f.bad} of ${f.total} aircraft in the cell` : null;
  const emitter =
    typeof f.emitter_lat === "number" && typeof f.emitter_lon === "number"
      ? `${fmtLatLon(f.emitter_lat, f.emitter_lon)}${typeof f.ellipse_major_km === "number" ? ` (± ${Math.round(f.ellipse_major_km / 2)} km)` : ""}`
      : null;
  const supporting = a.corroborations.filter((c) => c.relation !== "context");
  const adjust = a.corroborations.filter((c) => c.relation === "context");
  // newest evidence first, one line per rule: repeated windows of the same rule add no information
  const seenRule = new Set<string>();
  const rules = [...evs]
    .sort((x, y) => (x.window_end < y.window_end ? 1 : -1))
    .flatMap((e) => e.rules.filter((r) => r.passed))
    .filter((r) => !seenRule.has(r.rule) && Boolean(seenRule.add(r.rule)));
  const ents = [...new Set(evs.flatMap((e) => e.entities))].map((e) => ({ id: e, ...entityLabel(e) }));
  const checks = CHECKS[a.kind] ?? [];
  const open = stageOf(a.status) !== "closed";
  return [
    h(
      "div",
      { class: `next-step st-${stageOf(a.status)}` },
      h("b", {}, d.demo ? "Demo: " : "Next step: "),
      d.demo ? "raised by Heimdall from the recorded data of this case." : nextStep(a),
      open && checks.length
        ? h("ul", { class: "checks" }, ...checks.map((c) => h("li", {}, c)))
        : null,
    ),
    kv([
      ["Detected", kindLabel(a.kind)],
      ["Area", aoiName(a.aoi)],
      ["Position", a.lat !== null && a.lon !== null ? `${fmtLatLon(a.lat, a.lon)} · MGRS ${toMgrs(a.lat, a.lon)}` : null],
      ["First seen", fmtTime(a.first_seen)],
      ["Last seen", `${fmtTime(a.last_seen)} (${dur})`],
      ["Sources", sourceLine(evs)],
      ["Also seen by", supporting.length ? supporting.map((c) => `${DOMAIN_NAME[c.domain] ?? c.domain}: ${c.note}`).join("; ") : null],
      ["Data", ev ? (a.kind.startsWith("mil_") ? `${ev.coverage.n_observed} aircraft observed` : `${ev.coverage.n_observed} aircraft observed, ${ev.coverage.n_quality} with GPS accuracy data`) : null],
      ["Affected", affected],
      ["Estimated emitter", emitter],
      ["Confidence", `${Math.round(a.confidence * 100)} %${adjust.length ? ` (${adjust.map((c) => c.note).join("; ")})` : ""}`],
    ]),
    h("h3", {}, "Why it was flagged"),
    rules.length ? h("ul", { class: "why" }, ...rules.map((r) => h("li", {}, ruleLine(r)))) : h("p", { class: "note" }, "Rule values not recorded."),
    h("h3", {}, `Affected ${ents.some((e) => e.kind === "vessel") ? (ents.some((e) => e.kind === "aircraft") ? "aircraft and vessels" : "vessels") : "aircraft"} (${ents.length})`),
    ents.length
      ? h(
          "div",
          { class: "row ents" },
          ...ents.map((e) =>
            h(
              "button",
              { type: "button", class: `chip ent-${e.kind}`, title: "Show its recent track", "aria-pressed": String(state.selectedEntity === e.id), onclick: () => patch({ selectedEntity: e.id }) },
              icon(e.kind === "vessel" ? "ship" : "plane", 14),
              e.text,
            ),
          ),
        )
      : h("p", { class: "note" }, "None listed (area-wide detection)."),
  ];
}

let menu: "" | "false" | "shelve" = "";

function footer(d: AlertDetail): HTMLElement {
  const a = d.alert;
  if (d.demo) return h("div", { class: "foot" }, h("p", { class: "note" }, "Demo alert from a recorded case: read only."));
  const run = async (st: AlertStatus, reason?: string, until?: string): Promise<void> => {
    menu = "";
    error = (await triage(a.id, st, reason, until)) ?? "";
    renderInspector(document.getElementById("inspector")!);
  };
  const toggle = (m: typeof menu): void => {
    menu = menu === m ? "" : m;
    renderInspector(document.getElementById("inspector")!);
  };
  const btn = (label: string, title: string, fn: () => void, cls = "tri", ic?: Parameters<typeof icon>[0]): HTMLElement =>
    h("button", { type: "button", class: cls, title, onclick: fn }, ic ? icon(ic) : null, label);
  const stage = stageOf(a.status);
  const actions: HTMLElement[] = [];
  if (a.status === "new") actions.push(btn(T.ack, "Take ownership: I am checking this (a)", () => void run("acknowledged"), "tri primary", "ack"));
  if (stage === "action" || stage === "progress") {
    actions.push(btn("Confirm real", "The checks show real activity: keep tracking it", () => void run("confirmed"), stage === "progress" ? "tri primary" : "tri", "confirm"));
    actions.push(btn("False alarm", "Close as a false alarm (choose why)", () => toggle("false"), "tri", "dismiss"));
    actions.push(btn("Shelve", "Hide it for a few hours, then it comes back", () => toggle("shelve"), "tri", "shelve"));
  }
  if (stage === "progress" || stage === "confirmed") {
    actions.push(btn("Close: ended", "Real activity that has now stopped", () => void run("resolved"), stage === "confirmed" ? "tri primary" : "tri", "ack"));
  }
  if (a.status === "resolved" || a.status === "dismissed" || a.status === "confirmed") {
    actions.push(btn("Reopen", "Back to In progress", () => void run("under_review"), "tri", "undo"));
  }
  const sub =
    menu === "false"
      ? h("div", { class: "row tri-menu", role: "group", "aria-label": "False alarm reason" }, h("span", { class: "note" }, "Why?"), ...FALSE_ALARM_REASONS.map((r) => btn(r, "Close as a false alarm with this reason", () => void run("dismissed", r))))
      : menu === "shelve"
        ? h(
            "div",
            { class: "row tri-menu", role: "group", "aria-label": "Shelve for" },
            h("span", { class: "note" }, "Shelve for"),
            ...SHELVE_HOURS.map((hrs) => btn(`${hrs} h`, `Hidden for ${hrs} hours, then back to In progress`, () => void run("shelved", `Shelved ${hrs} h by analyst`, new Date(Date.now() + hrs * 3600_000).toISOString()))),
          )
        : null;
  return h(
    "div",
    { class: "foot" },
    actions.length ? h("div", { class: "row tri-row" }, ...actions) : h("p", { class: "note" }, "Ended on its own: nothing to do."),
    sub,
    error && h("div", { class: "fail", role: "alert" }, error),
  );
}

export function renderInspector(root: HTMLElement): void {
  const d = state.detail;
  if (!d || d.alert.id !== state.selected) {
    root.replaceChildren(h("div", { class: "empty" }, T.selectPrompt));
    return;
  }
  const a = d.alert;
  const pr = PRIORITY[a.severity]!;
  const prev = root.querySelector<HTMLElement>(".body")?.scrollTop ?? 0;
  root.replaceChildren(
    h(
      "div",
      { class: "head" },
      h("h2", {}, `${kindLabel(a.kind)} · ${aoiName(a.aoi)}`),
      h(
        "div",
        { class: "row" },
        h("span", { class: `badge sev-${pr.p === "P1" ? "critical" : "medium"}` }, `${pr.icon} ${pr.label}`),
        h("span", { class: "badge" }, STATUS_LABEL[a.status] ?? a.status),
        a.ref ? h("span", { class: "note" }, a.ref) : null,
      ),
    ),
    h("div", { class: "body inspector-body" }, ...body(d)),
    footer(d),
  );
  root.querySelector<HTMLElement>(".body")!.scrollTop = prev;
}

/** Entity view (aircraft or vessel): last fixes and the selected alert. */
export function renderEntity(root: HTMLElement, id: string): void {
  const tr = state.tracks.find((t) => t.hex === id);
  root.replaceChildren(
    h("div", { class: "head" }, h("h2", {}, entityLabel(id).text), iconButton("undo", "Back to alert", () => patch({ selectedEntity: null }))),
    h(
      "div",
      { class: "body" },
      tr
        ? table(["Time", "Lat", "Lon", "Altitude (ft)", "Speed (kn)"], tr.path.slice(-8).reverse().map((p) => [fmtTime(p.ts), p.lat.toFixed(3), p.lon.toFixed(3), fmtNum(p.alt, 0), fmtNum(p.gs, 0)]))
        : h("div", { class: "note" }, "No recent track loaded."),
    ),
  );
}
