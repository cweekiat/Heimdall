// Modal overlays: help (?) and shift summary with local Markdown download.
import type { Alert } from "./api";
import { fmtTime, h } from "./dom";
import type { IconName } from "./icons";
import { createWindow, EDGE_LEFT, EDGE_TOP, closeWindow, getBody, openWindow } from "./wm";
import { aoiName, CORROBORATION_LABEL, kindLabel, SOURCE_LABEL, T, truthLabel } from "./labels";
import { PRIORITY, STATUS_LABEL } from "./panels";
import { state } from "./store";
import { startTour } from "./tour/overlay";

export const SHORTCUTS: [string, string][] = [
  ["/", "Search alerts"],
  ["j / k", "Next / previous alert"],
  ["Enter", "Open the highlighted alert"],
  ["a", "Acknowledge"],
  ["r", "Start review"],
  ["c", "Confirm"],
  ["d", "Dismiss (asks for a reason)"],
  ["s", "Snooze (reason and until)"],
  ["n", "Add note"],
  ["f", "Fly to selection"],
  ["1 - 5", "Jump to area 1-5"],
  ["q", "Alerts window"],
  ["b", "Areas window"],
  ["l", "Layers & filters window"],
  ["u", "Data sources window"],
  ["x", "Actions window"],
  ["t", "Timeline window"],
  ["h", "Shift report window"],
  ["Space", "Play / pause timeline"],
  ["Esc", "Close the focused window"],
  ["Alt + arrows", "Nudge the focused window"],
  ["?", "This help"],
];

const DETECTORS: [string, string][] = [
  ["GPS jamming", "Many aircraft in one area report poor GPS accuracy, far above what is normal for that area. It only fires when enough aircraft are seen and the problem lasts more than one check."],
  ["GPS spoofing (fake position)", "Aircraft whose reported position jumps or disagrees with other sources. Needs several aircraft to agree before it alerts."],
  ["Military aircraft circling", "Military aircraft flying repeated loops or racetracks over a small area for a long time."],
  ["Several signs in one place", "Different kinds of data agree on the same place and time. Agreement raises how likely the alert is to be true."],
];

const GRADES: [string, string][] = Object.entries(SOURCE_LABEL).map(([k, v]) => [k, `Source: ${v}`]);
const CRED: [string, string][] = [1, 2, 3, 4, 5, 6].map((n) => [String(n), truthLabel(n)]);

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
    h("p", {}, "Priorities: ▲▲ Critical, ▲ High, ■ Medium, ● Low. Colour is never the only cue. Heimdall does not label anything hostile or friendly."),
    h("p", {}, "Areas: green means few aircraft affected, amber some, red many. Grey outline: too little data to judge (no data is not no activity)."),
    h("h2", {}, "How much to trust an alert"),
    h("p", {}, "Every alert has two badges: how reliable the source is, and how likely the information is true."),
    dl(GRADES),
    dl(CRED),
    h("p", { class: "note" }, "\"No second source yet\" never means false."),
    h("h2", {}, "What the alerts mean"),
    ...DETECTORS.flatMap(([k, v]) => [h("h3", {}, k), h("p", {}, v)]),
    h("p", { class: "note" }, "Windows: drag the title bar to move, drag edges to resize. Reset layout in the toolbar brings them back."),
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
        out.push(`  - ${PRIORITY[a.severity]!.label} · ${kindLabel(a.kind)} · ${SOURCE_LABEL[a.admiralty.reliability] ?? "Unknown"} source, ${truthLabel(a.admiralty.credibility).toLowerCase()} · ${CORROBORATION_LABEL[a.corroboration_status] ?? a.corroboration_status} · ${STATUS_LABEL[a.status] ?? a.status} · first ${fmtTime(a.first_seen)}`);
      }
    }
    return out.length ? out : ["- none"];
  };
  return [
    `# Heimdall shift report`,
    ``,
    `Generated ${new Date(now).toISOString().slice(0, 16).replace("T", " ")}Z, covering the last ${hours} h. Public-source information, unconfirmed until a second source agrees.`,
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
