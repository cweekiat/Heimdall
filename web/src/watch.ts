// Spectrum watch (task 033/035-lite): areas x bands status board, each cell judged against its own
// history, plus an Investigate pane for the selected cell. The watch-floor entry point:
// Monitor (board) -> Detect (state) -> Investigate (pane: value vs typical, explanations, alerts, next steps).
// All text via textContent (h helper); SVG built with createElementNS.
import { api } from "./api";
import type { Alert } from "./api";
import { h } from "./dom";
import { filters, setFilter } from "./filters";
import { createWindow, EDGE_LEFT } from "./wm";
import {
  BAND_NAME,
  NEXT_STEPS,
  STATE_HELP,
  STATE_LABEL,
  cellSentence,
  fmtValue,
  parseStatus,
  sparkPath,
} from "./spectrummodel";
import type { BandCell, SpectrumStatus, StatusArea, StatusBand } from "./spectrummodel";

const SVG = "http://www.w3.org/2000/svg";

function spark(vals: (number | null)[], w: number, h: number, cls = "spark"): SVGElement {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", String(w));
  svg.setAttribute("height", String(h));
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG, "path");
  p.setAttribute("d", sparkPath(vals, w, h));
  svg.append(p);
  return svg;
}

export interface WatchHooks {
  /** Fly the map to an area. */
  flyTo: (aoi: string) => void;
  /** Open an alert's details. */
  openAlert: (id: string) => void;
  /** Current alerts (for the related-alerts list). */
  alerts: () => Alert[];
  /** Open the GNSS layer / other layer when a next step asks for it. */
  showLayer?: (band: string) => void;
}

export class SpectrumWatch {
  readonly body: HTMLElement;
  status: SpectrumStatus | null = null;
  private sel: { aoi: string; band: string } | null = null;
  private error = "";
  onStatus: (s: SpectrumStatus | null) => void = () => undefined;

  constructor(private hooks: WatchHooks) {
    this.body = createWindow({
      id: "watch",
      title: "Spectrum watch",
      icon: "activity",
      x: EDGE_LEFT + 8,
      y: -12,
      w: 760,
      h: 360,
      minW: 360,
      minH: 200,
    });
    this.render();
  }

  start(): void {
    void this.refresh();
    window.setInterval(() => void this.refresh(), 60_000);
  }

  async refresh(): Promise<void> {
    try {
      this.status = parseStatus(await api.spectrumStatus());
      this.error = "";
    } catch (e) {
      this.error = `Spectrum status unavailable (${String(e).slice(0, 60)})`;
    }
    this.onStatus(this.status);
    this.render();
  }

  private pick(aoi: string, band: string): void {
    const same = this.sel?.aoi === aoi && this.sel.band === band;
    this.sel = same ? null : { aoi, band };
    if (!same) {
      setFilter({ band });
      this.hooks.flyTo(aoi);
    }
    this.render();
  }

  private cell(a: StatusArea, b: StatusBand): HTMLElement {
    const c = a.bands[b.band];
    if (!c) return h("td", { class: "wcell st-no_coverage" }, "–");
    const on = this.sel?.aoi === a.aoi && this.sel.band === b.band;
    const btn = h(
      "button",
      {
        type: "button",
        class: `wcell-btn st-${c.state}`,
        "aria-pressed": String(on),
        title: `${a.label} · ${BAND_NAME[b.band] ?? b.band}: ${STATE_LABEL[c.state]}. ${cellSentence(c, b)}`,
        onclick: () => this.pick(a.aoi, b.band),
      },
      h("span", { class: "wstate" }, STATE_LABEL[c.state]),
      h("span", { class: "wval num" }, fmtValue(c.value, b.unit)),
      spark(c.spark, 56, 14),
    );
    return h("td", { class: "wcell" }, btn);
  }

  private investigate(): HTMLElement | null {
    const s = this.status;
    if (!s || !this.sel) return null;
    const a = s.areas.find((x) => x.aoi === this.sel!.aoi);
    const b = s.bands.find((x) => x.band === this.sel!.band);
    const c: BandCell | undefined = a?.bands[this.sel.band];
    if (!a || !b || !c) return null;
    const related = this.hooks
      .alerts()
      .filter((al) => al.aoi === a.aoi && (al.bands ?? []).includes(b.band) && ["new", "acknowledged", "under_review"].includes(al.status));
    return h(
      "section",
      { class: "winvest", "aria-label": "Investigate" },
      h("h3", {}, `${BAND_NAME[b.band] ?? b.band} over ${a.label}`),
      h("div", { class: `wbadge st-${c.state}` }, STATE_LABEL[c.state]),
      h("p", {}, cellSentence(c, b)),
      h("p", { class: "note" }, `${STATE_HELP[c.state]} Based on ${c.n} observations now and ${c.n_history} past readings.`),
      h("div", { class: "wbig" }, spark(c.spark, 300, 48, "spark big"), h("div", { class: "note" }, "Last 24 h (hourly), this area")),
      h("h4", {}, "Natural explanations checked"),
      c.explanations.length
        ? h("ul", {}, ...c.explanations.map((x) => h("li", {}, x)))
        : h("p", { class: "note" }, b.band === "hf" || b.band === "l1_gnss" ? "Space weather is quiet: no storm or radio blackout in the last 6 h." : "None apply to this band."),
      h("h4", {}, `Open alerts here in this band (${related.length})`),
      related.length
        ? h("ul", {}, ...related.map((al) => h("li", {}, h("button", { type: "button", class: "ctx-link", onclick: () => this.hooks.openAlert(al.id) }, `${al.ref ?? ""} ${al.title}`.trim()))))
        : h("p", { class: "note" }, "None."),
      h("h4", {}, "Next steps"),
      h("ol", {}, ...(NEXT_STEPS[b.band] ?? []).map((t) => h("li", {}, t))),
    );
  }

  render(): void {
    const s = this.status;
    if (!s) {
      this.body.replaceChildren(h("div", { class: "note" }, this.error || "Loading spectrum status…"));
      return;
    }
    const head = h("tr", {}, h("th", { scope: "col" }, "Area"), ...s.bands.map((b) => h("th", { scope: "col", title: b.label }, BAND_NAME[b.band] ?? b.band)));
    const rows = s.areas.map((a) => h("tr", {}, h("th", { scope: "row" }, a.label), ...s.bands.map((b) => this.cell(a, b))));
    const legend = h(
      "div",
      { class: "wlegend" },
      ...(["anomalous", "elevated", "normal", "learning", "no_coverage"] as const).map((k) => h("span", { class: `wkey st-${k}`, title: STATE_HELP[k] }, STATE_LABEL[k])),
    );
    const inv = this.investigate();
    this.body.replaceChildren(
      h(
        "div",
        { class: `watch ${inv ? "with-inv" : ""}` },
        h(
          "div",
          { class: "wboard" },
          h("p", { class: "note" }, "Each cell compares an area's band with its own history. Click a cell to investigate; the map and alert list follow."),
          h("div", { class: "wtable-wrap" }, h("table", { class: "wtable" }, h("thead", {}, head), h("tbody", {}, ...rows))),
          legend,
          filters.band ? h("button", { type: "button", onclick: () => { setFilter({ band: "" }); this.sel = null; this.render(); } }, "Show all bands") : null,
        ),
        inv,
      ),
    );
  }
}
