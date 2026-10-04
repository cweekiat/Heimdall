// Detector tuning window (task 061, rail "Tuning", key k). How often each detector was right, judged by the
// analysts' own verdicts (Confirm real / False alarm with a reason), and threshold changes those verdicts support.
// Read-only: a proposal shows the config line to change; nothing is applied from the browser.
import { api } from "./api";
import { h } from "./dom";
import { fmtDT } from "./tz";
import { DETECTOR_NAME, needText, precisionText } from "./tuningmodel";
import type { TuningReport } from "./tuningmodel";
import { createWindow, EDGE_LEFT, EDGE_TOP } from "./wm";

export class TuningWindow {
  readonly body: HTMLElement;
  private data: TuningReport | null = null;
  private days = 30;
  private error = "";

  constructor() {
    this.body = createWindow({ id: "tuning", title: "Detector tuning", icon: "activity", x: EDGE_LEFT + 8, y: EDGE_TOP + 8, w: 520, h: 640, minW: 340 });
    this.render();
  }

  async refresh(): Promise<void> {
    try {
      this.data = await api.tuning(this.days);
      this.error = "";
    } catch (e) {
      this.error = `Tuning report unavailable (${String(e).slice(0, 60)})`;
    }
    this.render();
  }

  private render(): void {
    const d = this.data;
    const days = h(
      "select",
      { "aria-label": "Period", onchange: (e: Event) => ((this.days = Number((e.target as HTMLSelectElement).value)), void this.refresh()) },
      ...[7, 30, 90].map((n) => h("option", { value: String(n), selected: n === this.days }, `Last ${n} days`)),
    );
    const head = h("div", { class: "row" }, days, h("button", { type: "button", onclick: () => void this.refresh() }, "Refresh"));
    const intro = h(
      "p",
      { class: "note" },
      "Built from your verdicts: alerts closed as Confirm real or False alarm (with a reason). Proposals keep every confirmed alert and are only suggestions: change config/config.yaml and restart to apply.",
    );
    if (!d) {
      this.body.replaceChildren(h("div", { class: "body tuning" }, head, intro, h("p", { class: "note" }, this.error || "Loading…")));
      return;
    }
    const rows = d.detectors.map((x) =>
      h(
        "section",
        { class: "mil-sec tune-det" },
        h("h4", {}, DETECTOR_NAME[x.detector] ?? x.detector),
        h("p", {}, `${x.alerts} alerts · ${precisionText(x)} · ${x.unreviewed} not reviewed`),
        Object.keys(x.reasons).length
          ? h("p", { class: "note" }, "False-alarm reasons: ", Object.entries(x.reasons).map(([k, v]) => `${k} (${v})`).join(" · "))
          : null,
        ...x.proposals.map((p) =>
          h(
            "div",
            { class: "tune-prop" },
            h("b", {}, `${p.config}: ${p.current} → ${p.proposed}`),
            h("p", { class: "note" }, `${p.why}. Estimate on reviewed alerts only.`),
            h("code", {}, p.yaml),
            h("button", { type: "button", onclick: () => void navigator.clipboard?.writeText(p.yaml).catch(() => undefined) }, "Copy line"),
          ),
        ),
        ...x.hints.map((t) => h("p", { class: "note" }, `Hint: ${t}`)),
        x.proposals.length ? null : h("p", { class: "note" }, needText(x, d.rules) || "No threshold change would remove enough false alarms without losing a confirmed alert."),
      ),
    );
    this.body.replaceChildren(
      h(
        "div",
        { class: "body tuning" },
        head,
        intro,
        this.error ? h("p", { class: "note warn" }, this.error) : null,
        h("p", {}, h("b", {}, `${d.reviewed} of ${d.alerts}`), " alerts reviewed in the period · ", fmtDT(d.generated)),
        ...(rows.length ? rows : [h("p", { class: "note" }, "No alerts in the period.")]),
        h("p", { class: "note" }, d.limits),
      ),
    );
  }
}
