// AOI board: one card per area of interest. Click = fly to AOI + filter the queue.
import { ago, fmtNum, h } from "./dom";
import { aoiName, coverageLabel, kindLabel, T } from "./labels";
import { sparkline } from "./panels";
import { api } from "./api";
import { refreshAll, selectAoi, state } from "./store";

const SEV_ICON: Record<string, string> = { critical: "▲▲ Critical", high: "▲ High", medium: "■ Medium", low: "● Low", info: "● Low" };

const expanded = new Set<string>();

export function renderBoard(root: HTMLElement): void {
  root.replaceChildren(
    ...state.summary.map((s) => {
      const kinds = Object.entries(s.open_by_kind).map(([k, n]) => `${kindLabel(k)} ${n}`).join(", ");
      const trend =
        s.ratio_now !== null && s.ratio_median !== null ? (s.ratio_now > s.ratio_median * 1.2 ? "↑" : s.ratio_now < s.ratio_median * 0.8 ? "↓" : "→") : "";
      const open = expanded.has(s.name);
      return h(
        "div",
        { class: "aoirow" },
        h(
          "button",
          { class: "aoicard", "aria-pressed": String(state.selectedAoi === s.name), "data-aoi": s.name, onclick: () => selectAoi(s.name) },
          h(
            "div",
            { class: "t" },
            h("span", {}, aoiName(s.name)),
            h("span", {}, s.max_severity ? `${SEV_ICON[s.max_severity] ?? ""} · ` : "", `${s.open_total} open ${trend}`),
          ),
          sparkline(s.spark, s.ratio_median),
          open &&
            h(
              "div",
              { class: "note" },
              `${fmtNum(s.ratio_now === null ? null : s.ratio_now * 100, 1)}% of aircraft affected now (normal ${fmtNum(s.ratio_median === null ? null : s.ratio_median * 100, 1)}%)`,
            ),
          open && h("div", { class: "note" }, `${T.dataCoverage(coverageLabel((s.coverage ?? 0) / 40))} · ${kinds || "no open alerts"} · changed ${ago(s.last_change)}`),
        ),
        h(
          "button",
          {
            class: "exp",
            "aria-expanded": String(open),
            "aria-label": `${open ? "Collapse" : "Expand"} ${aoiName(s.name)} details`,
            onclick: () => {
              if (open) expanded.delete(s.name);
              else expanded.add(s.name);
              renderBoard(root);
            },
          },
          open ? "▴" : "▾",
        ),
      );
    }),
    ...(state.summary.length ? [] : [h("div", { class: "empty" }, "No area data yet.")]),
    clearControl(root),
  );
}

// Two-step in-page confirmation (no window.confirm). Clearing expires alerts; it is not a verdict.
let confirming = false;
let clearMsg = "";

function clearControl(root: HTMLElement): HTMLElement {
  const total = state.alerts.filter((a) => ["new", "acknowledged", "under_review", "shelved"].includes(a.status)).length;
  const redraw = (): void => renderBoard(root);
  if (!confirming) {
    return h(
      "div",
      { class: "clear-all" },
      h("button", { type: "button", class: "btn-clear", disabled: total === 0, onclick: () => { confirming = true; clearMsg = ""; redraw(); } }, `${T.clearAll} (${total})`),
      clearMsg && h("span", { class: "note", role: "status" }, clearMsg),
    );
  }
  return h(
    "div",
    { class: "clear-all confirm", role: "alertdialog", "aria-label": T.clearAll },
    h("span", {}, `Clear ${total} open alerts? They are marked expired, not dismissed, and the change is logged.`),
    h(
      "button",
      {
        type: "button",
        class: "btn-clear danger",
        onclick: () => {
          void api.clearAlerts().then(
            async (r) => {
              confirming = false;
              clearMsg = `Cleared ${r.cleared} alerts.`;
              await refreshAll();
              redraw();
            },
            () => {
              confirming = false;
              clearMsg = "Could not clear alerts.";
              redraw();
            },
          );
        },
      },
      "Yes, clear all",
    ),
    h("button", { type: "button", class: "btn-clear", onclick: () => { confirming = false; redraw(); } }, "Cancel"),
  );
}
