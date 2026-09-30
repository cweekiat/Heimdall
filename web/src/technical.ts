// "Technical details" disclosure: the only place raw terms (Admiralty letters, H3 cell, ratios,
// thresholds, baselines) may appear. Collapsed by default. Excluded from the banned-words check.
import type { AlertDetail, Evidence } from "./api";
import { fmtNum, fmtTime, h } from "./dom";
import { table } from "./panels";

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

export function technicalDetails(d: AlertDetail): HTMLElement {
  const a = d.alert;
  return h(
    "details",
    { class: "tech" },
    h("summary", {}, "Technical details"),
    h("p", {}, a.summary),
    table(
      ["Field", "Value"],
      [
        ["Admiralty grade", `${a.admiralty.reliability}${a.admiralty.credibility}`],
        ["H3 cell", a.h3_cell ?? "-"],
        ["Position", a.lat !== null && a.lon !== null ? `${a.lat.toFixed(3)}, ${a.lon.toFixed(3)}` : "-"],
        ["Confidence", fmtNum(a.confidence, 2)],
        ["First seen", fmtTime(a.first_seen)],
        ["Last seen", fmtTime(a.last_seen)],
      ],
    ),
    ...d.evidence.flatMap((ev) => [
      h("h4", {}, `${ev.detector} v${ev.detector_version} · persistence ${ev.persistence} window(s)`),
      rulesTable(ev),
      h(
        "div",
        { class: "note" },
        `Coverage: ${ev.coverage.n_quality} quality / ${ev.coverage.n_observed} observed via ${ev.coverage.providers.join(", ") || "-"}; confidence ${(ev.coverage.confidence * 100).toFixed(0)}%`,
      ),
      h("div", {}, ...ev.records.map((r) => h("span", { class: "chip", title: r.url ?? "" }, `${r.source}:${r.id}`))),
    ]),
  );
}
