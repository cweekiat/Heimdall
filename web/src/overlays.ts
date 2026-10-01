// Modal overlays: help (?) and shift summary with local Markdown download.
import { fmtDT } from "./tz";
import type { Alert } from "./api";
import { fmtTime, h } from "./dom";
import type { IconName } from "./icons";
import { createWindow, EDGE_LEFT, EDGE_TOP, closeWindow, getBody, openWindow } from "./wm";
import { aoiName, kindLabel, T } from "./labels";
import { sourceNames } from "./explain";
import { PRIORITY, STATUS_LABEL } from "./panels";
import { state } from "./store";
import { startTour } from "./tour/overlay";

export const SHORTCUTS: [string, string][] = [
  ["/", "Search (areas, alerts, flights, ships, lat, lon)"],
  ["j / k", "Next / previous alert"],
  ["Enter", "Open the highlighted alert"],
  ["a", "Acknowledge the selected alert"],
  ["d", "Clear the selected alert"],
  ["f", "Fly to selection"],
  ["1 - 5", "Jump to area 1-5"],
  ["q", "Alerts"],
  ["o", "Area of Ops"],
  ["b", "Area status board"],
  ["w", "Spectrum watch"],
  ["l", "Filters"],
  ["g", "3D globe / 2D map"],
  ["0", "Recenter on South-East Asia"],
  [",", "Settings"],
  ["u", "Data sources window"],
  ["t", "Timeline window"],
  ["h", "Shift report window"],
  ["Space", "Play / pause timeline"],
  ["Esc", "Close the focused window"],
  ["Alt + arrows", "Nudge the focused window"],
  ["?", "This help"],
];

const DETECTORS: [string, string][] = [
  ["GPS jamming", "Many aircraft in one area report poor GPS accuracy, far above what is normal for that area. It only fires when enough aircraft are seen and the problem lasts more than one check."],
  ["GPS spoofing", "An aircraft's reported position jumps away and stays wrong, or disagrees with radio timing or the local altitude pattern, while it still reports good GPS accuracy."],
  ["Military aircraft circling", "Military aircraft flying repeated loops or racetracks over a small area for a long time."],
  ["Several signs in one place", "Different kinds of data agree on the same place and time. Agreement raises how likely the alert is to be true."],
];


function open(id: string, title: string, ic: IconName, ...body: (Node | string | false)[]): void {
  createWindow({ id, title, icon: ic, w: 460, h: 520, x: EDGE_LEFT + 60, y: EDGE_TOP + 20 });
  const el = getBody(id)!;
  el.replaceChildren(...body.filter((x): x is Node | string => x !== false).map((x) => (typeof x === "string" ? document.createTextNode(x) : x)));
  openWindow(id);
}

const dl = (rows: [string, string][]): HTMLElement =>
  h("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [h("dt", {}, h("kbd", {}, k)), h("dd", {}, v)]));

export function showHelp(): void {
  open(
    "help",
    T.help,
    "help",
    h("button", { type: "button", class: "tour-start", onclick: () => { closeWindow("help"); startTour(); } }, "Take the guided tour"),
    h("h2", {}, "Keyboard shortcuts"),
    dl(SHORTCUTS),
    h("h2", {}, "Reading the map"),
    h("p", {}, "Two alert categories: ▲ Critical (look now; a small card pops up on the map) and ● Warning. Colour is never the only cue. Heimdall does not label anything hostile or friendly."),
    h("p", {}, "Areas: green means few aircraft affected, amber some, red many. Grey outline: too little data to judge (no data is not no activity)."),
    h("h2", {}, "Confidence"),
    h("p", {}, "Each alert shows a confidence percentage: how much data covered the area and how many independent sources agree. The alert lists its sources by name and the rule values that made it fire."),
    h("h2", {}, "What the alerts mean"),
    ...DETECTORS.flatMap(([k, v]) => [h("h3", {}, k), h("p", {}, v)]),
    h("p", { class: "note" }, "Windows: drag the title bar to move, drag edges to resize. Settings > Reset window layout brings them back."),
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
        out.push(`  - ${PRIORITY[a.severity]!.label} · ${kindLabel(a.kind)} · confidence ${Math.round(a.confidence * 100)} % · sources ${sourceNames(a)} · ${STATUS_LABEL[a.status] ?? a.status} · first ${fmtTime(a.first_seen)}`);
      }
    }
    return out.length ? out : ["- none"];
  };
  return [
    `# Heimdall shift report`,
    ``,
    `Generated ${fmtDT(now)}, covering the last ${hours} h. Public-source information.`,
    ``,
    `## Opened (${opened.length})`,
    ...byAoi(opened),
    ``,
    `## Closed: confirmed, dismissed or expired (${closed.length})`,
    `_Uses last-seen time; exact times are in each alert's notes._`,
    ...byAoi(closed),
    ``,
    `## Currently snoozed (${shelved.length})`,
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
  open("shift", T.shift, "shift", h("h2", {}, T.shift), h("div", { class: "row" }, sel, h("button", { class: "primary", onclick: download }, "Download Markdown (local file)")), h("pre", {}, md));
}
