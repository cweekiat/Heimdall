// AOI board: one card per area of interest. Click = fly to AOI + filter the queue.
import { ago, fmtNum, h } from "./dom";
import { aoiName, kindLabel } from "./labels";
import { sparkline } from "./panels";
import { selectAoi, state } from "./store";

const SEV_ICON: Record<string, string> = { critical: "▲▲ P1", high: "▲ P2", medium: "■ P3", low: "● P4", info: "● P4" };

export function renderBoard(root: HTMLElement): void {
  root.replaceChildren(
    h("h2", {}, "Areas of interest"),
    ...state.summary.map((s) => {
      const kinds = Object.entries(s.open_by_kind).map(([k, n]) => `${kindLabel(k)} ${n}`).join(", ");
      const trend =
        s.ratio_now !== null && s.ratio_median !== null ? (s.ratio_now > s.ratio_median * 1.2 ? "↑" : s.ratio_now < s.ratio_median * 0.8 ? "↓" : "→") : "";
      return h(
        "button",
        { class: "aoicard", "aria-pressed": String(state.selectedAoi === s.name), "data-aoi": s.name, onclick: () => selectAoi(s.name) },
        h(
          "div",
          { class: "t" },
          h("span", {}, aoiName(s.name), s.role === "control" ? " (control)" : ""),
          h("span", {}, s.max_severity ? `${SEV_ICON[s.max_severity] ?? ""} · ` : "", `${s.open_total} open`),
        ),
        sparkline(s.spark, s.ratio_median),
        h(
          "div",
          { class: "note" },
          `degraded ${fmtNum(s.ratio_now === null ? null : s.ratio_now * 100, 1)}% now ${trend} vs 14 d median ${fmtNum(s.ratio_median === null ? null : s.ratio_median * 100, 1)}%`,
        ),
        h("div", { class: "note" }, `coverage ${fmtNum(s.coverage, 0)} ac/h · ${kinds || "no open alerts"} · changed ${ago(s.last_change)}`),
      );
    }),
    ...(state.summary.length ? [] : [h("div", { class: "empty" }, "No AOI data.")]),
  );
}
