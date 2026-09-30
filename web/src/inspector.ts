// Right-hand inspector: header + Now / Why / Trend / Log tabs + triage footer.
// Every view surfaces the explanation: rules (value vs threshold vs baseline), coverage,
// Admiralty grade, corroboration and counterfactual. Actions come only from /api/playbook ids.
import { api } from "./api";
import type { ActionRec, AlertDetail, AlertStatus, Evidence, Note } from "./api";
import { ago, fmtNum, fmtTime, h } from "./dom";
import { alertTitle, aoiName, kindLabel } from "./labels";
import { chart, PRIORITY, STATUS_LABEL, table } from "./panels";
import { addNote, loadDetail, patch, state, triage } from "./store";

const ADMIRALTY_R: Record<string, string> = {
  A: "completely reliable",
  B: "usually reliable",
  C: "fairly reliable",
  D: "not usually reliable",
  E: "unreliable",
  F: "reliability cannot be judged",
};
const ADMIRALTY_C: Record<number, string> = {
  1: "confirmed by other sources",
  2: "probably true (independent domains agree)",
  3: "possibly true",
  4: "doubtful",
  5: "improbable",
  6: "truth cannot be judged",
};

type Pending = null | { kind: "dismissed" | "shelved" | "note" };
let pending: Pending = null;
let pendingFor: string | null = null;
let error = "";

export function openTriage(kind: "dismissed" | "shelved" | "note"): void {
  pending = { kind };
  pendingFor = state.selected;
  patch({ tab: kind === "note" ? "log" : state.tab });
  renderInspector(document.getElementById("inspector")!);
}

// ------------------------------------------------------------------ tabs

function rulesTable(ev: Evidence): HTMLElement {
  return table(
    ["Rule", "Value", "Threshold", "Baseline", "Result"],
    ev.rules.map((r) => [
      h("div", {}, h("div", {}, r.rule), h("div", { class: "note" }, r.description)),
      h("span", { class: "num" }, fmtNum(r.value)),
      h("span", { class: "num" }, r.threshold === null ? "n/a" : `${r.op} ${fmtNum(r.threshold)}`),
      h("span", { class: "num" }, fmtNum(r.baseline)),
      h("span", { class: r.passed ? "pass" : "fail" }, r.passed ? "PASS" : "FAIL"),
    ]),
  );
}

function tabNow(d: AlertDetail): Node[] {
  const a = d.alert;
  const ev = d.evidence[0];
  const fresh = ev ? ago(ev.window_end) : "-";
  return [
    h("p", {}, a.summary),
    table(
      ["Fact", "Value"],
      [
        ["Kind", kindLabel(a.kind)],
        ["Area", aoiName(a.aoi)],
        ["Cell", a.h3_cell ?? "-"],
        ["Position", a.lat !== null && a.lon !== null ? `${a.lat.toFixed(3)}, ${a.lon.toFixed(3)}` : "-"],
        ["First seen", fmtTime(a.first_seen)],
        ["Last seen", `${fmtTime(a.last_seen)} (${ago(a.last_seen)})`],
        ["Persistence", ev ? `${ev.persistence} window(s)` : "-"],
        ["Latest evidence", fresh],
      ],
    ),
    h("h3", {}, "Entities"),
    h(
      "div",
      { class: "row" },
      ...(d.evidence.flatMap((e) => e.entities).length
        ? [...new Set(d.evidence.flatMap((e) => e.entities))].map((e) =>
            h("button", { class: "chip", onclick: () => patch({ selectedEntity: e }), "aria-pressed": String(state.selectedEntity === e) }, e),
          )
        : [h("span", { class: "note" }, "No entities attached.")]),
    ),
    h("h3", {}, "Sources"),
    h(
      "div",
      { class: "row" },
      ...d.evidence.flatMap((e) => e.records.map((r) => h("span", { class: "chip", title: r.url ?? "" }, `${r.source}:${r.id}`))),
    ),
    h("h3", {}, "Recommended actions"),
    a.recommended_actions.length ? h("ul", {}, ...a.recommended_actions.map((x) => h("li", {}, x))) : h("div", { class: "note" }, "None."),
  ];
}

function tabWhy(d: AlertDetail): Node[] {
  const a = d.alert;
  return [
    h("h3", {}, "Admiralty grade"),
    h(
      "div",
      {},
      h("span", { class: "badge grade" }, `${a.admiralty.reliability}${a.admiralty.credibility}`),
      ` ${a.admiralty.reliability} = ${ADMIRALTY_R[a.admiralty.reliability] ?? "?"}; ${a.admiralty.credibility} = ${ADMIRALTY_C[a.admiralty.credibility] ?? "?"}.`,
    ),
    ...d.evidence.flatMap((ev) => [
      h("h3", {}, `Rules fired: ${ev.detector} v${ev.detector_version}`),
      rulesTable(ev),
      h("h3", {}, "Coverage"),
      h(
        "div",
        {},
        `${ev.coverage.n_quality} quality / ${ev.coverage.n_observed} observed via ${ev.coverage.providers.join(", ") || "-"}; coverage confidence ${(ev.coverage.confidence * 100).toFixed(0)}%`,
      ),
      h("div", { class: "bar" }, h("i", { style: `width:${ev.coverage.confidence * 100}%` })),
      h("h3", {}, "Counterfactual"),
      h("div", { class: "cf" }, ev.counterfactual ?? "Not provided by detector."),
      h("h3", {}, "Known gaps"),
      ev.gaps.length ? h("ul", {}, ...ev.gaps.map((g) => h("li", {}, g))) : h("div", { class: "note" }, "None recorded (this does not mean there are none)."),
    ]),
    h("h3", {}, "Corroboration"),
    h("span", { class: `badge ${a.corroboration_status}` }, a.corroboration_status),
    a.corroborations.length
      ? table(["Domain", "Relation", "Note"], a.corroborations.map((c) => [c.domain, c.relation, c.note]))
      : h("div", { class: "note" }, "Uncorroborated: no other domain has reported on this yet. That is not evidence it is false."),
  ];
}

let trendHours = 24;

function tabTrend(d: AlertDetail): Node[] {
  const a = d.alert;
  const host = h("div", {}, h("div", { class: "note" }, "Loading metric history..."));
  const rule = d.evidence[0]?.rules.find((r) => r.baseline !== null || r.threshold !== null);
  if (a.h3_cell) {
    api
      .metric(a.h3_cell, "degraded_ratio", trendHours)
      .then((pts) =>
        pts.length
          ? host.replaceChildren(
              chart(pts, rule?.baseline ?? null, rule?.threshold ?? null),
              h("div", { class: "note" }, `degraded ratio, last ${trendHours} h (blue) vs baseline (grey dashed) and threshold (amber dotted)`),
            )
          : host.replaceChildren(h("div", { class: "note" }, "No metric history recorded for this cell.")),
      )
      .catch(() => host.replaceChildren(h("div", { class: "note" }, "Metric history unavailable.")));
  } else host.replaceChildren(h("div", { class: "note" }, "This alert has no cell; no metric trend."));
  const ev = d.evidence[0];
  const onset = new Date(a.first_seen).getTime();
  const hrs = Math.max(0, (Date.now() - onset) / 3600_000);
  return [
    h(
      "div",
      { class: "row" },
      ...[24, 168].map((hh) =>
        h("button", { "aria-pressed": String(trendHours === hh), onclick: () => { trendHours = hh; renderInspector(document.getElementById("inspector")!); } }, hh === 24 ? "24 h" : "7 d"),
      ),
    ),
    host,
    h("h3", {}, "Projection"),
    h(
      "div",
      {},
      `Onset ${fmtTime(a.first_seen)} (${hrs.toFixed(1)} h ago); persisted ${ev?.persistence ?? 0} window(s). `,
      ev?.counterfactual ? `Clears if: ${ev.counterfactual}` : "No clearing condition provided.",
    ),
  ];
}

function logEntry(n: Note): HTMLElement {
  const tag = n.kind === "status" ? `status -> ${STATUS_LABEL[n.status_to ?? ""] ?? n.status_to}` : n.kind === "shelve" ? `shelved until ${fmtTime(n.shelve_until)}` : n.kind;
  return h("div", { class: "log" }, h("time", {}, `${fmtTime(n.ts)} · ${n.user} · ${tag}`), n.text);
}

function tabLog(d: AlertDetail): Node[] {
  const items: { ts: string; el: HTMLElement }[] = [
    ...d.notes.map((n) => ({ ts: n.ts, el: logEntry(n) })),
    ...d.actions.map((x) => ({ ts: x.ts, el: actionRow(x) })),
    ...d.feedback.map((f) => ({
      ts: f.ts,
      el: h("div", { class: "log" }, h("time", {}, `${fmtTime(f.ts)} · ${f.user} · verdict`), `${f.verdict}${f.reason ? `: ${f.reason}` : ""}`),
    })),
  ].sort((x, y) => y.ts.localeCompare(x.ts));
  return items.length ? items.map((i) => i.el) : [h("div", { class: "empty" }, "No journal entries yet. Use Note to record your reasoning for the next watch.")];
}

function actionRow(x: ActionRec): HTMLElement {
  const act = (fn: () => Promise<unknown>) => async () => {
    await fn();
    if (state.selected) await loadDetail(state.selected);
  };
  return h(
    "div",
    { class: "log" },
    h("time", {}, `${fmtTime(x.ts)} · action`),
    h("span", { class: "chip" }, x.action_id),
    " ",
    h("span", { class: "badge" }, x.status),
    x.status === "proposed" && h("button", { class: "primary", onclick: act(() => api.approve(x.id)) }, "Approve"),
    x.status === "proposed" && h("button", { onclick: act(() => api.reject(x.id)) }, "Reject"),
  );
}

// ------------------------------------------------------------------ footer (triage + playbook)

const isoLocal = (hours: number): string => new Date(Date.now() + hours * 3600_000).toISOString().slice(0, 16);

function footer(d: AlertDetail): HTMLElement {
  const a = d.alert;
  const t = (s: AlertStatus, label: string, cls = "") =>
    h("button", { class: cls, disabled: a.status === s, onclick: () => void run(triage(a.id, s)) }, label);
  const run = async (p: Promise<string | null>): Promise<void> => {
    error = (await p) ?? "";
    pending = null;
    renderInspector(document.getElementById("inspector")!);
  };
  const form = (): HTMLElement | null => {
    if (!pending || pendingFor !== a.id) return null;
    const text = h("input", {
      type: "text",
      "aria-label": pending.kind === "note" ? "Note" : "Reason",
      placeholder: pending.kind === "note" ? "Analyst note" : "Reason (required)",
      id: "pending-text",
    });
    const until = h("input", { type: "datetime-local", "aria-label": "Shelve until (UTC)", value: isoLocal(4) });
    const kind = pending.kind;
    const submit = () => {
      const v = text.value.trim();
      if (!v) {
        error = kind === "note" ? "Note is empty" : "A reason is required";
        renderInspector(document.getElementById("inspector")!);
        return;
      }
      if (kind === "note") void run(addNote(a.id, v));
      else if (kind === "dismissed") void run(triage(a.id, "dismissed", v));
      else void run(triage(a.id, "shelved", v, new Date(until.value + "Z").toISOString()));
    };
    text.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submit();
      if (e.key === "Escape") {
        pending = null;
        renderInspector(document.getElementById("inspector")!);
      }
    });
    setTimeout(() => text.focus(), 0);
    return h(
      "div",
      { class: "row" },
      text,
      kind === "shelved" && h("label", {}, "until (UTC) ", until),
      h("button", { class: "primary", onclick: submit }, kind === "note" ? "Add note" : kind === "dismissed" ? "Dismiss" : "Shelve"),
      h("button", { onclick: () => { pending = null; renderInspector(document.getElementById("inspector")!); } }, "Cancel"),
    );
  };
  const pb = d.playbook.filter((p) => p.applies_to.length === 0 || p.applies_to.includes(a.kind));
  return h(
    "div",
    { class: "foot" },
    h(
      "div",
      { class: "row" },
      t("acknowledged", "Ack"),
      t("under_review", "Review"),
      t("confirmed", "Confirm", "primary"),
      h("button", { class: "danger", onclick: () => openTriage("dismissed") }, "Dismiss…"),
      h("button", { onclick: () => openTriage("shelved") }, "Shelve…"),
      h("button", { onclick: () => openTriage("note") }, "Note…"),
    ),
    form(),
    error && h("div", { class: "fail", role: "alert" }, error),
    pb.length > 0 &&
      h(
        "div",
        { class: "row" },
        h("span", { class: "note" }, "Playbook:"),
        ...pb.map((p) =>
          h(
            "button",
            {
              title: `${p.id} (risk: ${p.risk})`,
              onclick: async () => {
                await api.propose(a.id, p.id).catch((e) => { error = String(e); });
                await loadDetail(a.id);
              },
            },
            `Propose: ${p.label}${p.risk === "high" ? " [high risk]" : ""}`,
          ),
        ),
      ),
  );
}

// ------------------------------------------------------------------ main

export function renderInspector(root: HTMLElement): void {
  const d = state.detail;
  if (!d || d.alert.id !== state.selected) {
    root.replaceChildren(h("div", { class: "empty" }, "Select an alert from the queue or map to see why it fired."));
    return;
  }
  if (pendingFor !== d.alert.id) {
    pending = null;
    error = "";
  }
  const a = d.alert;
  const pr = PRIORITY[a.severity]!;
  const prev = root.querySelector<HTMLElement>(".body")?.scrollTop ?? 0;
  const tabs: [typeof state.tab, string][] = [["now", "Now"], ["why", "Why"], ["trend", "Trend"], ["log", `Log (${d.notes.length + d.actions.length})`]];
  const content = state.tab === "now" ? tabNow(d) : state.tab === "why" ? tabWhy(d) : state.tab === "trend" ? tabTrend(d) : tabLog(d);
  const typing = document.activeElement instanceof HTMLInputElement && root.contains(document.activeElement) ? document.activeElement : null;
  const draft = typing?.value;
  root.replaceChildren(
    h(
      "div",
      { class: "head" },
      h("h2", {}, `${pr.icon} ${pr.p} ${alertTitle(a)}`),
      h(
        "div",
        { class: "row" },
        h("span", { class: `badge ${a.corroboration_status}` }, a.corroboration_status),
        h("span", { class: "badge grade", title: "Admiralty: reliability A-F x credibility 1-6" }, `Admiralty ${a.admiralty.reliability}${a.admiralty.credibility}`),
        h("span", { class: "badge" }, STATUS_LABEL[a.status] ?? a.status),
        h("span", { class: "note" }, `conf ${(a.confidence * 100).toFixed(0)}%`),
        h("button", { "aria-label": "Close inspector", onclick: () => patch({ selected: null, detail: null, tracks: [] }) }, "Close"),
      ),
      h("div", { class: "bar", role: "progressbar", "aria-valuenow": String(Math.round(a.confidence * 100)), "aria-valuemin": "0", "aria-valuemax": "100", "aria-label": "Confidence" }, h("i", { style: `width:${a.confidence * 100}%` })),
    ),
    h(
      "div",
      { class: "tabs", role: "tablist" },
      ...tabs.map(([k, l]) => h("button", { role: "tab", "aria-selected": String(state.tab === k), onclick: () => patch({ tab: k }) }, l)),
    ),
    h("div", { class: "body", role: "tabpanel" }, ...content),
    footer(d),
  );
  root.querySelector<HTMLElement>(".body")!.scrollTop = prev;
  if (draft) {
    const t = root.querySelector<HTMLInputElement>("#pending-text");
    if (t) t.value = draft;
  }
}

/** Entity inspector (aircraft / hex): last fixes and the alerts that reference it. */
export function renderEntity(root: HTMLElement, hex: string): void {
  const refs = state.alerts.filter((a) => a.id === state.selected);
  const tr = state.tracks.find((t) => t.hex === hex);
  root.replaceChildren(
    h("div", { class: "head" }, h("h2", {}, `Entity ${hex}`), h("button", { onclick: () => patch({ selectedEntity: null }) }, "Back")),
    h(
      "div",
      { class: "body" },
      tr
        ? table(["Time", "Lat", "Lon", "Alt", "GS"], tr.path.slice(-8).reverse().map((p) => [fmtTime(p.ts), p.lat.toFixed(3), p.lon.toFixed(3), fmtNum(p.alt, 0), fmtNum(p.gs, 0)]))
        : h("div", { class: "note" }, "No track loaded."),
      h("h3", {}, "Referenced by"),
      ...refs.map((a) => h("div", {}, alertTitle(a))),
    ),
  );
}
