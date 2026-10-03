// Demo window (task 040): steps through a real-data replay scenario. Each step jumps the replay clock to a recorded
// moment, moves the map and opens Heimdall's alert for it. Text comes from the server and is set via textContent.
import { api } from "./api";
import type { Scenario, ScenarioStep } from "./api";
import { h } from "./dom";
import { fmtDT } from "./tz";
import { createWindow, EDGE_LEFT, openWindow } from "./wm";

export const STATUS_LABEL: Record<ScenarioStep["status"], string> = {
  seen: "Seen in data",
  not_visible: "Not visible on ADS-B",
  no_data: "No data in this replay",
};

export interface ScenarioHooks {
  jumpTo: (lon: number, lat: number, zoom: number) => void;
  openAlert: (id: string) => void;
  /** Refresh layers after the clock moved. */
  refresh: () => void;
  /** 043: show a step (live server: draw its snapshot). Default: seek the replay clock (demo server). */
  step?: (s: ScenarioStep) => void | Promise<void>;
  /** 043: leave the demo (live server); shows an Exit button when set. */
  exit?: () => void;
}

export class ScenarioPanel {
  readonly body: HTMLElement;
  private cur = -1;
  private busy = false;

  constructor(
    private sc: Scenario,
    private hooks: ScenarioHooks,
  ) {
    this.body = createWindow({ id: "scenario", title: "Demo", icon: "timeline", x: EDGE_LEFT + 8, y: 64, w: 420, h: 620, minW: 300 });
    this.render();
    openWindow("scenario");
  }

  /** 043: load another scenario into the same window (live Demo page). */
  setScenario(sc: Scenario): void {
    this.sc = sc;
    this.cur = -1;
    this.render();
    openWindow("scenario");
  }

  async go(i: number): Promise<void> {
    const s = this.sc.steps[i];
    if (!s || this.busy) return;
    this.busy = true;
    this.cur = i;
    this.render();
    try {
      await (this.hooks.step ? this.hooks.step(s) : api.replaySeek(s.at));
    } catch (e) {
      console.error("demo step failed", e);
    }
    // keep the subject right of this window: move the centre west by half the window width
    const win = this.body.closest(".win")?.getBoundingClientRect();
    const px = win && window.innerWidth >= 768 ? win.right / 2 : 0;
    this.hooks.jumpTo(s.lon - (px * 360) / (512 * 2 ** s.zoom), s.lat, s.zoom);
    this.hooks.refresh();
    this.busy = false;
  }

  render(): void {
    const steps = this.sc.steps.map((s, i) =>
      h(
        "button",
        { type: "button", class: `sc-step st-${s.status}`, "aria-current": String(i === this.cur), onclick: () => void this.go(i) },
        h("span", { class: "sc-when" }, `${i + 1}. ${s.when}`),
        h("span", { class: "sc-status" }, STATUS_LABEL[s.status]),
        h("b", { class: "sc-title" }, s.title),
        i === this.cur ? h("span", { class: "sc-brief" }, `Brief: ${s.brief}`) : null,
        i === this.cur ? h("span", { class: "sc-saw" }, `Heimdall: ${s.saw}`) : null,
        i === this.cur && s.examples.length ? h("span", { class: "sc-ex" }, s.examples.join(" · ")) : null,
        i === this.cur ? h("span", { class: "sc-at" }, `Map shows ${fmtDT(s.at)}`) : null,
      ),
    );
    const cur = this.sc.steps[this.cur];
    const open =
      cur?.alert &&
      h("button", { type: "button", class: "btn sc-alert", onclick: () => cur.alert && this.hooks.openAlert(cur.alert) }, "Open Heimdall's alert for this step");
    const nav = h(
      "div",
      { class: "sc-nav" },
      this.hooks.exit ? h("button", { type: "button", class: "btn", onclick: () => this.hooks.exit?.() }, "Exit demo") : null,
      h("button", { type: "button", class: "btn", onclick: () => void this.go(Math.max(0, this.cur - 1)), disabled: this.cur <= 0 }, "Back"),
      h("button", { type: "button", class: "btn primary", onclick: () => void this.go(Math.min(this.sc.steps.length - 1, this.cur + 1)), disabled: this.cur >= this.sc.steps.length - 1 }, this.cur < 0 ? "Start" : "Next"),
    );
    this.body.replaceChildren(
      h("div", { class: "sc-head" }, h("b", {}, this.sc.title), h("div", { class: "note" }, this.sc.summary)),
      nav,
      ...steps.flatMap((el, i) => (i === this.cur && open ? [el, open] : [el])),
      h("div", { class: "note warn" }, this.sc.caveat),
      h("div", { class: "note" }, `Sources: ${this.sc.sources}`),
    );
  }
}
