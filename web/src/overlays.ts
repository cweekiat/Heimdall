// Modal overlays: help (?) and shift summary with local Markdown download.
import type { Alert } from "./api";
import { fmtTime, h } from "./dom";
import { alertTitle, aoiName, kindLabel } from "./labels";
import { PRIORITY, STATUS_LABEL } from "./panels";
import { state } from "./store";

export const SHORTCUTS: [string, string][] = [
  ["/", "Search alerts"],
  ["j / k", "Next / previous alert"],
  ["Enter", "Open the highlighted alert"],
  ["a", "Acknowledge"],
  ["r", "Mark under review"],
  ["c", "Confirm"],
  ["d", "Dismiss (asks for a reason)"],
  ["s", "Shelve (reason and until)"],
  ["n", "Add analyst note"],
  ["f", "Fly to selection"],
  ["1 - 5", "Jump to AOI 1-5"],
  ["l", "Layers drawer"],
  ["t", "Show / hide timeline"],
  ["Space", "Play / pause timeline"],
  ["Esc", "Close dialog, form or inspector"],
  ["?", "This help"],
];

const DETECTORS: [string, string][] = [
  ["GNSS jamming", "Share of aircraft reporting degraded navigation integrity (NIC/NACp) in a hex cell, compared with that cell's own 14-day baseline. It fires only when the ratio, the aircraft count and persistence over two or more windows all pass."],
  ["GNSS spoofing", "Aircraft whose reported position jumps or disagrees with other sources (impossible speed, offset from multilateration). Needs several aircraft to agree before it alerts."],
  ["Military orbit", "Military-flagged aircraft flying repeated loops or racetracks over a small area for an extended time."],
  ["Composite", "Several detectors or domains agree on the same place and time. Corroboration raises the Admiralty credibility digit."],
];

const GRADES: [string, string][] = [
  ["A", "completely reliable source"], ["B", "usually reliable"], ["C", "fairly reliable"],
  ["D", "not usually reliable"], ["E", "unreliable"], ["F", "reliability cannot be judged"],
];
const CRED: [string, string][] = [
  ["1", "confirmed by other sources"], ["2", "probably true"], ["3", "possibly true"],
  ["4", "doubtful"], ["5", "improbable"], ["6", "truth cannot be judged"],
];

export function closeModal(): boolean {
  const m = document.getElementById("modal")!;
  const was = !m.hidden;
  m.hidden = true;
  return was;
}
export const modalOpen = (): boolean => !document.getElementById("modal")!.hidden;

function open(...body: (Node | string | false)[]): void {
  const m = document.getElementById("modal")!;
  m.replaceChildren(
    h(
      "div",
      { class: "dlg", role: "dialog", "aria-modal": "true", "aria-label": "Dialog" },
      h("div", { class: "row" }, h("span", { class: "sb-spacer" }), h("button", { onclick: closeModal, "aria-label": "Close dialog" }, "Close")),
      ...body,
    ),
  );
  m.hidden = false;
  m.querySelector<HTMLButtonElement>("button")?.focus();
}

const dl = (rows: [string, string][]): HTMLElement =>
  h("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [h("dt", {}, h("kbd", {}, k)), h("dd", {}, v)]));

export function showHelp(): void {
  open(
    h("h2", {}, "Keyboard shortcuts"),
    dl(SHORTCUTS),
    h("h2", {}, "Legend"),
    h("p", {}, "Priorities: ▲▲ P1 critical, ▲ P2 high, ■ P3 medium, ● P4 low/info. Colour is never the only cue. Heimdall does not label anything hostile or friendly."),
    h("p", {}, "Hexes: green under 2% degraded, amber 2-10%, red over 10%. Grey outline: fewer than 3 quality aircraft (no data is not no activity)."),
    h("h2", {}, "Admiralty grade"),
    h("p", {}, "Letter = source reliability, digit = credibility of the information."),
    dl(GRADES),
    dl(CRED),
    h("p", { class: "note" }, "Uncorroborated means no other domain has reported it yet. It never means false."),
    h("h2", {}, "What the detectors do"),
    ...DETECTORS.flatMap(([k, v]) => [h("h3", {}, k), h("p", {}, v)]),
  );
}

// ------------------------------------------------------------------ shift summary

export function shiftMarkdown(hours: number, now = Date.now()): string {
  const since = now - hours * 3600_000;
  const inWin = (iso: string): boolean => new Date(iso).getTime() >= since;
  const opened = state.alerts.filter((a) => inWin(a.first_seen));
  const closed = state.alerts.filter((a) => ["confirmed", "dismissed", "expired"].includes(a.status) && inWin(a.last_seen));
  const shelved = state.alerts.filter((a) => a.status === "shelved");
  const stillOpen = state.alerts.filter((a) => ["new", "acknowledged", "under_review"].includes(a.status));
  const byAoi = (xs: Alert[]): string[] => {
    const out: string[] = [];
    for (const g of [...new Set(xs.map((a) => aoiName(a.aoi)))].sort()) {
      out.push(`- **${g}**`);
      for (const a of xs.filter((x) => aoiName(x.aoi) === g)) {
        out.push(`  - ${PRIORITY[a.severity]!.p} ${kindLabel(a.kind)} · grade ${a.admiralty.reliability}${a.admiralty.credibility} · ${a.corroboration_status} · ${STATUS_LABEL[a.status] ?? a.status} · first ${fmtTime(a.first_seen)}`);
      }
    }
    return out.length ? out : ["- none"];
  };
  return [
    `# Heimdall shift summary`,
    ``,
    `Generated ${new Date(now).toISOString().slice(0, 16).replace("T", " ")}Z, covering the last ${hours} h. Public-source OSINT, unverified until corroborated.`,
    ``,
    `## Opened (${opened.length})`,
    ...byAoi(opened),
    ``,
    `## Closed: confirmed, dismissed or expired (${closed.length})`,
    `_Uses last-seen time; exact status-change times are in each alert's log._`,
    ...byAoi(closed),
    ``,
    `## Currently shelved (${shelved.length})`,
    ...byAoi(shelved),
    ``,
    `## Still open at handover (${stillOpen.length})`,
    ...byAoi(stillOpen),
    ``,
  ].join("\n");
}

export function showShift(hours = 8): void {
  const md = shiftMarkdown(hours);
  const sel = h(
    "select",
    { "aria-label": "Hours covered", onchange: (e) => showShift(Number((e.target as HTMLSelectElement).value)) },
    ...[4, 8, 12, 24].map((n) => h("option", { value: String(n), selected: n === hours }, `Last ${n} h`)),
  );
  const download = (): void => {
    const url = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
    const a = h("a", { href: url, download: `heimdall-shift-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "")}.md` });
    document.body.append(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };
  open(h("h2", {}, "Shift summary"), h("div", { class: "row" }, sel, h("button", { class: "primary", onclick: download }, "Download Markdown (local file)")), h("pre", {}, md));
  void alertTitle;
}
