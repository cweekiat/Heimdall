// Alert details window: header + Now / Why / Trend / Log tabs + triage footer.
// Every view surfaces the explanation: what fired (value vs normal), data coverage, source and truth badges,
// corroboration and the "Clears when" line. Raw numbers live only under "Technical details".
// Actions come only from /api/playbook ids.
import { api } from "./api";
import type { ActionRec, AlertDetail, AlertStatus, Note } from "./api";
import { ago, fmtNum, fmtTime, h } from "./dom";
import { icon, iconButton } from "./icons";
import type { IconName } from "./icons";
import { alertTitle, aoiName, CORROBORATION_LABEL, confidenceLabel, coverageLabel, KIND_ICON, kindLabel, sourceLabel, T, truthLabel } from "./labels";
import { chart, PRIORITY, STATUS_LABEL, table } from "./panels";
import { explainBlock } from "./explain";
import { ctxSection } from "./context";
import { technicalDetails } from "./technical";
import { addNote, loadDetail, patch, state, triage } from "./store";

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

const pct = (v: number): string => `${v < 0.1 ? (v * 100).toFixed(1) : Math.round(v * 100)}%`;

/** One plain sentence describing what was seen versus the normal level. */
function plainSentence(d: AlertDetail): string {
  const ev = d.evidence[0];
  const rule = ev?.rules.find((r) => r.baseline !== null && r.value !== null);
  if (ev && rule && rule.value !== null && rule.baseline !== null && rule.value <= 1 && d.alert.kind === "gnss_jamming") {
    const total = ev.coverage.n_quality || ev.coverage.n_observed;
    const n = Math.round(rule.value * total);
    return total > 0 ? T.lostGps(n, total, pct(rule.value), pct(rule.baseline)) : T.affectedNow(pct(rule.value), pct(rule.baseline));
  }
  if (rule && rule.value !== null && rule.baseline !== null && rule.value <= 1) return T.affectedNow(pct(rule.value), pct(rule.baseline));
  return `${kindLabel(d.alert.kind)} near ${aoiName(d.alert.aoi)}.`;
}

function factRow(ic: IconName, text: string, tip: string): HTMLElement {
  return h("span", { class: "fact", title: tip }, icon(ic), text);
}

type Kid = Node | string | null | undefined | false;

function tabNow(d: AlertDetail): Kid[] {
  const a = d.alert;
  const ev = d.evidence[0];
  const covWord = ev ? coverageLabel(ev.coverage.confidence) : "Poor";
  const mins = Math.max(0, (new Date(a.last_seen).getTime() - new Date(a.first_seen).getTime()) / 60000);
  return [
    h(
      "div",
      { class: "facts" },
      factRow(KIND_ICON[a.kind] ?? "warn", kindLabel(a.kind), "Type"),
      factRow("area", aoiName(a.aoi), T.area),
      factRow("clock", T.since(ago(a.first_seen)), "When it started"),
      factRow("seen", T.seenFor(mins), "How long it has persisted"),
      factRow("coverage", T.dataCoverage(covWord), "How much data supports this"),
    ),
    explainBlock(a, d.evidence),
    ctxSection(a.id),
    h("p", { class: "plain" }, plainSentence(d)),
    ev?.counterfactual && h("p", { class: "clears" }, h("strong", {}, `${T.clearsWhen}: `), ev.counterfactual),
    h("h3", {}, "Things involved"),
    h(
      "div",
      { class: "row" },
      ...(d.evidence.flatMap((e) => e.entities).length
        ? [...new Set(d.evidence.flatMap((e) => e.entities))].map((e) =>
            h("button", { class: "chip", onclick: () => patch({ selectedEntity: e }), "aria-pressed": String(state.selectedEntity === e) }, e),
          )
        : [h("span", { class: "note" }, "None attached.")]),
    ),
    h("h3", {}, "Suggested next steps"),
    a.recommended_actions.length ? h("ul", {}, ...a.recommended_actions.map((x) => h("li", {}, x))) : h("div", { class: "note" }, "None."),
    technicalDetails(d),
  ];
}

function tabWhy(d: AlertDetail): Kid[] {
  const a = d.alert;
  const ev = d.evidence[0];
  const bullets: Node[] = [];
  for (const e of d.evidence) {
    for (const r of e.rules) {
      bullets.push(h("li", { class: r.passed ? "pass" : "fail" }, `${r.passed ? "Yes" : "No"}: ${r.description}`));
    }
  }
  return [
    h("h3", {}, "How much to trust it"),
    h(
      "div",
      { class: "row" },
      h("span", { class: "badge grade" }, sourceLabel(a.admiralty.reliability)),
      h("span", { class: "badge grade" }, truthLabel(a.admiralty.credibility)),
      h("span", { class: `badge ${a.corroboration_status}` }, CORROBORATION_LABEL[a.corroboration_status] ?? a.corroboration_status),
      h("span", { class: "note" }, `Confidence: ${confidenceLabel(a.confidence)}`),
    ),
    h("h3", {}, "What triggered it"),
    bullets.length ? h("ul", { class: "why" }, ...bullets) : h("div", { class: "note" }, "No rule details recorded."),
    ev && h("h3", {}, "Data coverage"),
    ev && h("div", {}, `${T.dataCoverage(coverageLabel(ev.coverage.confidence))} (${ev.coverage.n_quality} good reports out of ${ev.coverage.n_observed} seen)`),
    ev && h("div", { class: "bar" }, h("i", { style: `width:${ev.coverage.confidence * 100}%` })),
    ...d.evidence.flatMap((e) => [
      e.counterfactual && h("h3", {}, T.clearsWhen),
      e.counterfactual && h("div", { class: "cf" }, e.counterfactual),
      h("h3", {}, "What we cannot see"),
      e.gaps.length ? h("ul", {}, ...e.gaps.map((g) => h("li", {}, g))) : h("div", { class: "note" }, "None recorded (this does not mean there are none)."),
    ]),
    h("h3", {}, "Other sources"),
    a.corroborations.length
      ? table(["Type", "Agrees?", "Note"], a.corroborations.map((c) => [c.domain, c.relation, c.note]))
      : h("div", { class: "note" }, T.noSecondSource),
    technicalDetails(d),
  ];
}

let trendHours = 24;

function tabTrend(d: AlertDetail): Node[] {
  const a = d.alert;
  const host = h("div", {}, h("div", { class: "note" }, "Loading history..."));
  const rule = d.evidence[0]?.rules.find((r) => r.baseline !== null || r.threshold !== null);
  if (a.h3_cell) {
    api
      .metric(a.h3_cell, "degraded_ratio", trendHours)
      .then((pts) =>
        pts.length
          ? host.replaceChildren(
              chart(pts, rule?.baseline ?? null, rule?.threshold ?? null),
              h("div", { class: "note" }, `Share of aircraft affected, last ${trendHours} h (blue), against the normal level (grey dashed) and the alert line (amber dotted)`),
            )
          : host.replaceChildren(h("div", { class: "note" }, "No history recorded for this area.")),
      )
      .catch(() => host.replaceChildren(h("div", { class: "note" }, "History unavailable.")));
  } else host.replaceChildren(h("div", { class: "note" }, "This alert has no area, so no trend is available."));
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
    h("h3", {}, "Outlook"),
    h(
      "div",
      {},
      `Started ${fmtTime(a.first_seen)} (${hrs.toFixed(1)} h ago). `,
      ev?.counterfactual ? `${T.clearsWhen}: ${ev.counterfactual}` : "No clearing condition provided.",
    ),
  ];
}

function logEntry(n: Note): HTMLElement {
  const tag = n.kind === "status" ? `status: ${STATUS_LABEL[n.status_to ?? ""] ?? n.status_to}` : n.kind === "shelve" ? `shelved until ${fmtTime(n.shelve_until)}` : n.kind;
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
  return items.length ? items.map((i) => i.el) : [h("div", { class: "empty" }, "No notes yet. Add a note to record your reasoning for the next shift.")];
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
  const t = (s: AlertStatus, ic: IconName, label: string, cls = "") =>
    h("button", { class: `tri ${cls}`.trim(), title: label, disabled: a.status === s, onclick: () => void run(triage(a.id, s)) }, icon(ic), label);
  const tb = (ic: IconName, label: string, kind: "dismissed" | "shelved" | "note", cls = "") =>
    h("button", { class: `tri ${cls}`.trim(), title: label, onclick: () => openTriage(kind) }, icon(ic), label);
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
      h("button", { class: "primary", onclick: submit }, kind === "note" ? T.note : kind === "dismissed" ? T.dismiss : T.shelve),
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
      t("acknowledged", "ack", T.ack),
      t("under_review", "review", T.review),
      t("confirmed", "confirm", T.confirm, "primary"),
      tb("dismiss", T.dismiss, "dismissed", "danger"),
      tb("shelve", T.shelve, "shelved"),
      tb("note", T.note, "note"),
    ),
    form(),
    error && h("div", { class: "fail", role: "alert" }, error),
    pb.length > 0 &&
      h(
        "div",
        { class: "row" },
        h("span", { class: "note" }, "Suggested actions:"),
        ...pb.map((p) =>
          h(
            "button",
            {
              title: `${p.label} (risk: ${p.risk})`,
              onclick: async () => {
                await api.propose(a.id, p.id).catch((e) => { error = String(e); });
                await loadDetail(a.id);
              },
            },
            `Propose: ${p.label}${p.risk === "high" ? " (high risk)" : ""}`,
          ),
        ),
      ),
  );
}

// ------------------------------------------------------------------ main

export function renderInspector(root: HTMLElement): void {
  const d = state.detail;
  if (!d || d.alert.id !== state.selected) {
    root.replaceChildren(h("div", { class: "empty" }, T.selectPrompt));
    return;
  }
  if (pendingFor !== d.alert.id) {
    pending = null;
    error = "";
  }
  const a = d.alert;
  const pr = PRIORITY[a.severity]!;
  const prev = root.querySelector<HTMLElement>(".body")?.scrollTop ?? 0;
  const tabs: [typeof state.tab, string][] = [["now", "Now"], ["why", "Why"], ["trend", "Trend"], ["log", `Notes (${d.notes.length + d.actions.length})`]];
  const content = state.tab === "now" ? tabNow(d) : state.tab === "why" ? tabWhy(d) : state.tab === "trend" ? tabTrend(d) : tabLog(d);
  const typing = document.activeElement instanceof HTMLInputElement && root.contains(document.activeElement) ? document.activeElement : null;
  const draft = typing?.value;
  root.replaceChildren(
    h(
      "div",
      { class: "head" },
      h("h2", {}, `${pr.icon} ${alertTitle(a)}`),
      h(
        "div",
        { class: "row" },
        h("span", { class: `badge sev-${a.severity}` }, `${pr.label} priority`),
        h("span", { class: "badge grade" }, sourceLabel(a.admiralty.reliability)),
        h("span", { class: "badge grade" }, truthLabel(a.admiralty.credibility)),
        h("span", { class: `badge ${a.corroboration_status}` }, CORROBORATION_LABEL[a.corroboration_status] ?? a.corroboration_status),
        h("span", { class: "badge" }, STATUS_LABEL[a.status] ?? a.status),
        h("span", { class: "note" }, `Confidence: ${confidenceLabel(a.confidence)}`),
      ),
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
    h("div", { class: "head" }, h("h2", {}, `Aircraft ${hex}`), iconButton("undo", "Back to alert", () => patch({ selectedEntity: null }))),
    h(
      "div",
      { class: "body" },
      tr
        ? table(["Time", "Lat", "Lon", "Altitude", "Speed"], tr.path.slice(-8).reverse().map((p) => [fmtTime(p.ts), p.lat.toFixed(3), p.lon.toFixed(3), fmtNum(p.alt, 0), fmtNum(p.gs, 0)]))
        : h("div", { class: "note" }, "No track loaded."),
      h("h3", {}, "Mentioned in"),
      ...refs.map((a) => h("div", {}, alertTitle(a))),
    ),
  );
}
