import { api } from "./api";
import type { ActionRec, Alert, AlertDetail, Evidence, SourceHealth } from "./api";
import { ago, fmtNum, fmtTime, h } from "./dom";
import { alertTitle, aoiName, kindLabel } from "./labels";

const SEV_RANK: Record<string, number> = { critical: 4, high: 3, medium: 2, low: 1, info: 0 };
export const priority = (a: Alert): number => SEV_RANK[a.severity]! * a.confidence;

export interface QueueFilters {
  aoi: string;
  kind: string;
  status: string;
  grade: string;
}
export const emptyFilters = (): QueueFilters => ({ aoi: "", kind: "", status: "", grade: "" });

const uniq = (xs: (string | null)[]): string[] => [...new Set(xs.filter((x): x is string => !!x))].sort();

export function renderQueue(
  root: HTMLElement,
  alerts: Alert[],
  f: QueueFilters,
  selected: string | null,
  onSelect: (id: string) => void,
  onFilter: () => void,
): void {
  const sel = (label: string, key: keyof QueueFilters, opts: string[], name: (v: string) => string = (v) => v) =>
    h(
      "select",
      {
        "aria-label": label,
        onchange: (e) => {
          f[key] = (e.target as HTMLSelectElement).value;
          onFilter();
        },
      },
      h("option", { value: "" }, `${label}: all`),
      ...opts.map((o) => h("option", { value: o, selected: f[key] === o }, name(o))),
    );
  const shown = alerts
    .filter(
      (a) =>
        (!f.aoi || a.aoi === f.aoi) &&
        (!f.kind || a.kind === f.kind) &&
        (!f.status || a.status === f.status) &&
        (!f.grade || a.admiralty.reliability === f.grade),
    )
    .sort((a, b) => priority(b) - priority(a));
  root.replaceChildren(
    h("h2", {}, `Alerts (${shown.length})`),
    h(
      "div",
      { class: "filters" },
      sel("AOI", "aoi", uniq(alerts.map((a) => a.aoi)), aoiName),
      sel("Kind", "kind", uniq(alerts.map((a) => a.kind)), kindLabel),
      sel("Status", "status", ["new", "confirmed", "dismissed", "expired"]),
      sel("Grade", "grade", ["A", "B", "C", "D", "E", "F"]),
    ),
    ...(shown.length ? [] : [h("div", { class: "empty" }, "No alerts match. Absence of alerts is not proof of absence of activity.")]),
    ...shown.map((a) =>
      h(
        "button",
        {
          class: `card sev-${a.severity}`,
          "aria-pressed": String(a.id === selected),
          onclick: () => onSelect(a.id),
        },
        h("div", { class: "t" }, alertTitle(a)),
        h(
          "div",
          { class: "m" },
          h("span", {}, a.severity.toUpperCase()),
          h("span", {}, `conf ${(a.confidence * 100).toFixed(0)}%`),
          h("span", { class: "badge grade", title: "Admiralty grade (source reliability x credibility)" }, `${a.admiralty.reliability}${a.admiralty.credibility}`),
          h("span", { class: `badge ${a.corroboration_status}` }, a.corroboration_status),
          h("span", {}, a.status),
          h("span", {}, fmtTime(a.last_seen)),
        ),
      ),
    ),
  );
}

// ------------------------------------------------------------------ evidence card

function chart(points: { ts: string; value: number }[], baseline: number | null, threshold: number | null): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "chart");
  svg.setAttribute("viewBox", "0 0 300 90");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Metric over last 24 hours versus baseline and threshold");
  if (!points.length) return svg;
  const vals = points.map((p) => p.value);
  const max = Math.max(...vals, threshold ?? 0, baseline ?? 0, 0.01) * 1.1;
  const y = (v: number) => 85 - (v / max) * 78;
  const x = (i: number) => 4 + (i / Math.max(1, points.length - 1)) * 292;
  const line = (d: string, color: string, dash = "") => {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", color);
    p.setAttribute("stroke-width", "2");
    p.setAttribute("vector-effect", "non-scaling-stroke");
    if (dash) p.setAttribute("stroke-dasharray", dash);
    svg.append(p);
  };
  if (baseline !== null) line(`M4 ${y(baseline)} H296`, "#9fb0c0", "4 3");
  if (threshold !== null) line(`M4 ${y(threshold)} H296`, "#d29922", "2 3");
  line(points.map((p, i) => `${i ? "L" : "M"}${x(i)} ${y(p.value)}`).join(" "), "#58a6ff");
  return svg;
}

/** Responsive table: horizontal scroll inside its own wrapper; stacked label/value rows at <=720px. */
function table(headers: string[], rows: (Node | string)[][], cls = ""): HTMLElement {
  return h(
    "div",
    { class: "tw" },
    h(
      "table",
      { class: `stack ${cls}` },
      h("thead", {}, h("tr", {}, ...headers.map((t) => h("th", { scope: "col" }, t)))),
      h(
        "tbody",
        {},
        ...rows.map((r) =>
          h("tr", {}, ...r.map((c, i) => h("td", { "data-label": headers[i] ?? "" }, c))),
        ),
      ),
    ),
  );
}

function rulesTable(ev: Evidence): HTMLElement {
  return table(
    ["Rule", "Value", "Threshold", "Baseline", "Result"],
    ev.rules.map((r) => [
      h("div", {}, h("div", {}, r.rule), h("div", { class: "note" }, r.description)),
      h("span", { class: "num" }, fmtNum(r.value)),
      // Every rule shows its threshold with the operator (value op threshold), "n/a" if none defined.
      h("span", { class: "num" }, r.threshold === null ? "n/a" : `${r.op} ${fmtNum(r.threshold)}`),
      h("span", { class: "num" }, fmtNum(r.baseline)),
      h("span", { class: r.passed ? "pass" : "fail" }, r.passed ? "PASS" : "FAIL"),
    ]),
  );
}

export function renderDetail(root: HTMLElement, d: AlertDetail, onChange: () => void): void {
  const scroll = root.scrollTop;
  const a = d.alert;
  const chartHost = h("div", {}, h("div", { class: "note" }, "Loading metric history..."));
  const primary = d.evidence[0];
  if (a.h3_cell) {
    const rule = primary?.rules.find((r) => r.baseline !== null || r.threshold !== null);
    api
      .metric(a.h3_cell, "degraded_ratio")
      .then((pts) => {
        if (!pts.length) return chartHost.replaceChildren(h("div", { class: "note" }, "No metric history recorded for this cell."));
        chartHost.replaceChildren(
          chart(pts, rule?.baseline ?? null, rule?.threshold ?? null),
          h("div", { class: "note" }, "degraded ratio, last 24 h (blue) vs baseline (grey dashed) and threshold (amber dotted)"),
        );
      })
      .catch(() => chartHost.replaceChildren(h("div", { class: "note" }, "Metric history unavailable.")));
  } else chartHost.replaceChildren();

  const reason = h("input", { type: "text", placeholder: "Reason (e.g. known_training_area)", "aria-label": "Feedback reason" });
  const fb = (verdict: "confirm" | "dismiss") => async () => {
    await api.feedback(a.id, verdict, reason.value);
    onChange();
  };

  const actionRows = d.actions.map((x) => actionRow(x, onChange));
  const proposable = d.playbook;

  root.replaceChildren(
    h("h2", {}, alertTitle(a)),
    h(
      "div",
      { class: "row" },
      h("span", { class: `badge ${a.corroboration_status}` }, a.corroboration_status),
      h("span", { class: "badge grade", title: "Admiralty: reliability A-F x credibility 1-6" }, `Admiralty ${a.admiralty.reliability}${a.admiralty.credibility}`),
      h("span", { class: "badge" }, a.severity),
      h("span", { class: "badge" }, a.status),
    ),
    h("p", {}, a.summary),
    h("div", { class: "note" }, `Confidence ${(a.confidence * 100).toFixed(0)}% - first ${fmtTime(a.first_seen)} - last ${fmtTime(a.last_seen)}`),
    h("div", { class: "bar", role: "progressbar", "aria-valuenow": String(Math.round(a.confidence * 100)), "aria-valuemin": "0", "aria-valuemax": "100", "aria-label": "Confidence" }, h("i", { style: `width:${a.confidence * 100}%` })),

    ...d.evidence.flatMap((ev) => [
      h("h3", {}, `Rules fired - ${ev.detector} v${ev.detector_version}`),
      rulesTable(ev),
      h("h3", {}, "Coverage"),
      h(
        "div",
        {},
        `${ev.coverage.n_quality} quality / ${ev.coverage.n_observed} observed entities via ${ev.coverage.providers.join(", ") || "-"}; coverage confidence ${(ev.coverage.confidence * 100).toFixed(0)}%; persistence ${ev.persistence} window(s)`,
      ),
      h("div", { class: "bar" }, h("i", { style: `width:${ev.coverage.confidence * 100}%` })),
      h("h3", {}, "Counterfactual"),
      h("div", { class: "cf" }, ev.counterfactual ?? "Not provided by detector."),
      h("h3", {}, "Known gaps"),
      ev.gaps.length ? h("ul", {}, ...ev.gaps.map((g) => h("li", {}, g))) : h("div", { class: "note" }, "None recorded (this does not mean there are none)."),
      h("h3", {}, "Entities & records"),
      h("div", { class: "row" }, ...ev.entities.map((e) => h("span", { class: "chip" }, e)), ...ev.records.map((r) => h("span", { class: "chip", title: r.url ?? "" }, `${r.source}:${r.id}`))),
    ]),

    h("h3", {}, "Corroboration matrix"),
    a.corroborations.length
      ? table(
          ["Domain", "Relation", "Note"],
          a.corroborations.map((c) => [c.domain, c.relation, c.note]),
        )
      : h("div", { class: "note" }, "Uncorroborated: no other domain has reported on this yet. That is not evidence it is false."),

    h("h3", {}, "Metric vs baseline"),
    chartHost,

    h("h3", {}, "Actions board (playbook only)"),
    ...(actionRows.length ? actionRows : [h("div", { class: "note" }, "No actions proposed.")]),
    h(
      "div",
      { class: "row" },
      ...proposable.map((p) =>
        h(
          "button",
          {
            title: `${p.id} (risk: ${p.risk})`,
            onclick: async () => {
              await api.propose(a.id, p.id);
              onChange();
            },
          },
          `Propose: ${p.label}${p.risk === "high" ? " [high risk]" : ""}`,
        ),
      ),
    ),

    h("h3", {}, "Analyst verdict"),
    reason,
    h(
      "div",
      { class: "row" },
      h("button", { class: "primary", onclick: fb("confirm") }, "Confirm"),
      h("button", { class: "danger", onclick: fb("dismiss") }, "Dismiss"),
    ),
    ...d.feedback.map((f) => h("div", { class: "note" }, `${f.verdict} by ${f.user} ${fmtTime(f.ts)}${f.reason ? ` - ${f.reason}` : ""}`)),
  );
  root.scrollTop = scroll; // re-render (SSE / actions) must not jump or blank the view
}

function actionRow(x: ActionRec, onChange: () => void): HTMLElement {
  const btns =
    x.status === "proposed"
      ? [
          h("button", { class: "primary", onclick: async () => { await api.approve(x.id); onChange(); } }, "Approve"),
          h("button", { onclick: async () => { await api.reject(x.id); onChange(); } }, "Reject"),
        ]
      : [];
  return h("div", { class: "row" }, h("span", { class: "chip" }, x.action_id), h("span", { class: "badge" }, x.status), ...btns);
}

// ------------------------------------------------------------------ data health

export function renderHealth(root: HTMLElement, sources: SourceHealth[]): void {
  const grade = (s: SourceHealth): string => {
    const stale = s.last_ok ? (Date.now() - new Date(s.last_ok).getTime()) / 1000 > s.expected_latency_s * 5 : true;
    return stale || s.error_rate > 0.25 ? "fail" : s.error_rate > 0.05 ? "warn" : "pass";
  };
  const label: Record<string, string> = { pass: "OK", warn: "Degraded", fail: "Stale / failing" };
  root.replaceChildren(
    h("h2", {}, "Data health"),
    h("p", { class: "note" }, "Low freshness lowers alert confidence; a quiet source is a coverage gap, not a quiet region."),
    table(
      ["Source", "Domain", "Reliability", "Last OK", "Records/h", "Error rate", "Last error", "Status"],
      sources.map((s) => {
        const g = grade(s);
        return [
          s.source,
          s.domain,
          h("span", { class: "badge grade" }, s.reliability),
          ago(s.last_ok),
          h("span", { class: "num" }, String(s.records_last_hour)),
          h("span", { class: "num" }, `${(s.error_rate * 100).toFixed(1)}%`),
          s.last_error ? `${s.last_error} (${ago(s.last_error_at)})` : "-",
          h("span", { class: g === "warn" ? "" : g, style: g === "warn" ? "color:var(--warn);font-weight:700" : "" }, label[g] ?? g),
        ];
      }),
    ),
  );
}

