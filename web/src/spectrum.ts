// Spectrum UI (task 030, ADR-008): band ladder, spectrum map layers, GNSS daily map with date picker.
// Live layers (HF spots, SatNOGS passes) and daily-sweep layers (GNSS map, Sentinel-1 / SMAP interference) are drawn
// with deck.gl on the shared overlay. All text goes via textContent; nothing here fetches from third-party hosts.
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import { api } from "./api";
import type { SourceState } from "./api";
import { h } from "./dom";
import { filters, onFilters, setFilter } from "./filters";
import type { OpsMap } from "./map";
import { parseLayer, pointOf, ringOf } from "./p3model";
import type { LayerFC, LayerFeature } from "./p3model";
import { rgba } from "./theme/palette";
import {
  BANDS,
  DAILY_LAYERS,
  GNSS_LEGEND,
  GNSS_RGB,
  LIVE_LAYERS,
  SPECTRUM_HOURS,
  SPECTRUM_LABEL,
  SPECTRUM_LAYERS,
  SPECTRUM_REFRESH_MS,
  bandCounts,
  bandDot,
  gnssDateLabel,
  gnssHover,
  hfRadius,
  newestLabel,
  parseGnssDay,
  readHash,
  spectrumHover,
  stepDay,
  writeHash,
} from "./spectrummodel";
import type { GnssCell, GnssDay, SpectrumLayer } from "./spectrummodel";

type EventLayer = Exclude<SpectrumLayer, "gnss">;
const EMPTY: LayerFC = { type: "FeatureCollection", features: [], truncated: false };

export class SpectrumLayers {
  private data = new Map<EventLayer, LayerFC>();
  private gnss: GnssDay | null = null;
  private gnssMissing = false;
  private days: string[] = [];
  private tip: HTMLElement;
  private timers: number[] = [];
  private liveBox = h("div", { class: "spectrum-toggles" });
  private dailyBox = h("div", { class: "spectrum-toggles" });
  readonly ladder = h("nav", { class: "band-ladder", "aria-label": "RF bands" });
  private alerts: { bands?: string[] | null; status: string }[] = [];
  private sources: SourceState[] = [];
  private cov = new Map<string, number>();

  constructor(private ops: OpsMap, wrap: HTMLElement) {
    this.tip = h("div", { class: "hovercard tip", hidden: true });
    wrap.append(this.tip, this.ladder);
    let last = `${filters.spectrum.join()}|${filters.gnssDate}|${filters.band}`;
    onFilters(() => {
      const now = `${filters.spectrum.join()}|${filters.gnssDate}|${filters.band}`;
      if (now === last) return;
      const dateChanged = now.split("|")[1] !== last.split("|")[1];
      last = now;
      if (dateChanged) this.gnss = null;
      void this.refresh();
      this.drawPanels();
      this.drawLadder();
    });
    window.addEventListener("hashchange", () => this.applyHash());
  }

  start(): void {
    this.applyHash();
    this.drawPanels();
    this.drawLadder();
    void this.loadDays();
    void this.refresh();
    for (const l of SPECTRUM_LAYERS) this.timers.push(window.setInterval(() => void this.refresh([l]), SPECTRUM_REFRESH_MS[l]));
  }

  private applyHash(): void {
    const hsh = readHash(location.hash);
    const patch: { band?: string; gnssDate?: string } = {};
    if (hsh.band !== filters.band) patch.band = hsh.band;
    if (hsh.gnss && hsh.gnss !== filters.gnssDate) patch.gnssDate = hsh.gnss;
    if (Object.keys(patch).length) setFilter(patch);
  }

  private setHash(p: { band?: string; gnss?: string }): void {
    const next = writeHash(location.hash, p);
    if (next !== location.hash) history.replaceState(null, "", next || location.pathname + location.search);
  }

  private on(l: SpectrumLayer): boolean {
    return filters.spectrum.includes(l);
  }

  private async loadDays(): Promise<void> {
    try {
      this.days = (await api.gnssDays()).days;
    } catch {
      this.days = [];
    }
    this.drawPanels();
  }

  async refresh(only: SpectrumLayer[] = SPECTRUM_LAYERS): Promise<void> {
    await Promise.all(
      only.filter((l) => this.on(l)).map(async (l) => {
        if (l === "gnss") {
          try {
            this.gnss = parseGnssDay(await api.gnssDay(filters.gnssDate));
            this.gnssMissing = false;
          } catch {
            this.gnss = null;
            this.gnssMissing = true; // 404: no map for that day yet
          }
          return;
        }
        try {
          this.data.set(l, parseLayer(await api.layer(l, "all", SPECTRUM_HOURS[l])));
        } catch {
          if (!this.data.has(l)) this.data.set(l, EMPTY);
        }
      }),
    );
    this.drawPanels();
    this.ops.render();
  }

  // -------------------------------------------------------------- panel blocks (Live / Daily sweep)

  live(): HTMLElement {
    return this.liveBox;
  }

  daily(): HTMLElement {
    return this.dailyBox;
  }

  private toggle(l: SpectrumLayer, note: string): HTMLElement {
    const i = h("input", { type: "checkbox" });
    i.checked = this.on(l);
    i.addEventListener("change", () =>
      setFilter({ spectrum: i.checked ? [...filters.spectrum.filter((x) => x !== l), l] : filters.spectrum.filter((x) => x !== l) }),
    );
    return h("label", { class: "chk" }, i, SPECTRUM_LABEL[l], note ? h("span", { class: "note" }, ` · ${note}`) : null);
  }

  private gnssControls(): HTMLElement {
    const sel = h("select", { "aria-label": "GNSS map date (UTC)" });
    for (const d of ["last24h", ...[...this.days].reverse()]) {
      const o = h("option", { value: d }, d === "last24h" ? "Last 24 h" : `${d} UTC`);
      o.selected = d === filters.gnssDate;
      sel.append(o);
    }
    sel.addEventListener("change", () => this.pickDate(sel.value));
    const step = (dir: -1 | 1, label: string, text: string): HTMLElement =>
      h("button", { type: "button", "aria-label": label, onclick: () => this.pickDate(stepDay(this.days, filters.gnssDate, dir)) }, text);
    const legend = h(
      "div",
      { class: "gnss-legend" },
      ...GNSS_LEGEND.map(([c, t]) => h("span", { class: "gnss-key" }, h("i", { class: `sw gnss-${c}` }), t)),
    );
    const label = this.gnssMissing ? "No GNSS map for this date yet" : gnssDateLabel(this.gnss);
    return h(
      "div",
      { class: "gnss-controls" },
      h("div", { class: "row" }, step(-1, "Previous day", "‹"), sel, step(1, "Next day", "›")),
      h("div", { class: "note" }, label),
      legend,
      h("div", { class: "note" }, "Share of aircraft reporting poor GPS accuracy (GPSJam method). Not proof of jamming."),
    );
  }

  private pickDate(d: string): void {
    setFilter({ gnssDate: d });
    this.setHash({ gnss: d === "last24h" ? "" : d });
  }

  private drawPanels(): void {
    const note = (l: EventLayer): string => (this.on(l) ? newestLabel((this.data.get(l) ?? EMPTY).features) : "");
    this.liveBox.replaceChildren(...LIVE_LAYERS.map((l) => this.toggle(l, note(l as EventLayer))));
    this.dailyBox.replaceChildren(
      ...DAILY_LAYERS.map((l) => (l === "gnss" ? this.toggle(l, "") : this.toggle(l, note(l as EventLayer)))),
      this.on("gnss") ? this.gnssControls() : "",
    );
  }

  // -------------------------------------------------------------- band ladder

  /** Called by main on every data refresh: open-alert counts and source health per band. */
  update(alerts: { bands?: string[] | null; status: string }[], sources: SourceState[]): void {
    this.alerts = alerts;
    this.sources = sources;
    this.drawLadder();
  }

  private pickBand(b: string): void {
    const next = filters.band === b ? "" : b;
    setFilter({ band: next });
    this.setHash({ band: next });
  }

  private async coverage(band: string): Promise<void> {
    if (this.cov.has(band) || !["l1_gnss", "l_1090", "vhf_marine"].includes(band)) return;
    try {
      const r = (await api.coverage(band, this.ops.bbox())) as { cells?: { receivers?: number }[] };
      this.cov.set(band, (r.cells ?? []).reduce((s, c) => s + (c.receivers ?? 0), 0));
      window.setTimeout(() => this.cov.delete(band), 60_000);
      this.drawLadder();
    } catch {
      /* coverage is a hover nicety */
    }
  }

  private drawLadder(): void {
    const open = this.alerts.filter((a) => ["new", "acknowledged", "under_review"].includes(a.status));
    const counts = bandCounts(open);
    const rows = BANDS.map((b) => {
      const dot = bandDot(b, this.sources);
      const feeds = b.sources.join(", ");
      const cov = this.cov.get(b.band);
      const title = `${b.name} (${b.range}) · sources: ${feeds}${cov !== undefined ? ` · ${cov} receivers in view (last hour)` : ""}`;
      return h(
        "button",
        {
          type: "button",
          class: "band-row",
          title,
          "aria-pressed": String(filters.band === b.band),
          onclick: () => this.pickBand(b.band),
          onmouseenter: () => void this.coverage(b.band),
        },
        h("i", { class: `dot dot-${dot}`, "aria-label": dot === "green" ? "receiving" : dot === "amber" ? "source problem" : "no source yet" }),
        h("span", { class: "band-name" }, b.name),
        h("span", { class: "band-range note" }, b.range),
        h("span", { class: "band-count num" }, String(counts[b.band] ?? 0)),
      );
    });
    // narrow screens: the ladder collapses to a dropdown (CSS shows one or the other)
    const sel = h("select", { class: "band-select", "aria-label": "RF band filter" });
    sel.append(h("option", { value: "" }, "All bands"));
    for (const b of BANDS) {
      const o = h("option", { value: b.band }, `${b.name} (${counts[b.band] ?? 0})`);
      o.selected = filters.band === b.band;
      sel.append(o);
    }
    sel.addEventListener("change", () => {
      setFilter({ band: sel.value });
      this.setHash({ band: sel.value });
    });
    this.ladder.replaceChildren(h("div", { class: "band-title note" }, "RF bands"), ...rows, sel);
  }

  // -------------------------------------------------------------- map layers

  private hover(text: string | null, x: number, y: number): void {
    this.tip.hidden = !text;
    if (!text) return;
    this.tip.style.left = `${x + 12}px`;
    this.tip.style.top = `${y + 12}px`;
    this.tip.textContent = text;
  }

  private feats(l: EventLayer): LayerFeature[] {
    return this.on(l) ? (this.data.get(l) ?? EMPTY).features : [];
  }

  layers(tool: string): unknown[] {
    const pick = tool === "none";
    const out: unknown[] = [];
    if (this.on("gnss") && this.gnss?.cells.length) {
      out.push(
        new H3HexagonLayer<GnssCell>({
          id: "spec-gnss",
          data: this.gnss.cells,
          getHexagon: (d) => d.h3,
          getFillColor: (d) => [...GNSS_RGB[d.cls], d.cls === "green" ? 60 : 130] as [number, number, number, number],
          updateTriggers: { getFillColor: [this.gnss] },
          stroked: false,
          filled: true,
          extruded: false,
          pickable: pick,
          onHover: (i) => this.hover(i.object ? gnssHover(i.object as GnssCell) : null, i.x, i.y),
        }),
      );
    }
    const smap = this.feats("smap").filter((f) => typeof f.properties.h3 === "string");
    if (smap.length)
      out.push(
        new H3HexagonLayer<LayerFeature>({
          id: "spec-smap",
          data: smap,
          getHexagon: (d) => String(d.properties.h3),
          getFillColor: (d) => rgba("alert", 0.08 + 0.6 * Math.min(1, Number(d.properties.rfi_frac ?? 0))),
          stroked: false,
          filled: true,
          extruded: false,
          pickable: pick,
          onHover: (i) => this.hover(i.object ? spectrumHover("smap", i.object as LayerFeature) : null, i.x, i.y),
        }),
      );
    const sar = this.feats("sar_rfi").filter((f) => !!ringOf(f));
    if (sar.length)
      out.push(
        new PathLayer<LayerFeature>({
          id: "spec-sar",
          data: sar,
          getPath: (d) => ringOf(d) as [number, number][],
          getColor: (d) => (d.properties.rfi_detected === true ? rgba("alert", 0.95) : rgba("notice", 0.45)),
          widthMinPixels: 1.5,
        }),
        new ScatterplotLayer<LayerFeature>({
          id: "spec-sar-pick",
          data: sar,
          getPosition: (d) => pointOf(d)!,
          getRadius: 7,
          radiusUnits: "pixels",
          getFillColor: (d) => (d.properties.rfi_detected === true ? rgba("alert", 0.6) : rgba("notice", 0.2)),
          pickable: pick,
          onHover: (i) => this.hover(i.object ? spectrumHover("sar_rfi", i.object as LayerFeature) : null, i.x, i.y),
        }),
      );
    const hf = this.feats("hf").filter((f) => !!pointOf(f));
    if (hf.length)
      out.push(
        new ScatterplotLayer<LayerFeature>({
          id: "spec-hf",
          data: hf,
          getPosition: (d) => pointOf(d)!,
          getRadius: (d) => hfRadius(typeof d.properties.n === "number" ? d.properties.n : null),
          radiusUnits: "pixels",
          stroked: true,
          getFillColor: rgba("neutral", 0.25),
          getLineColor: rgba("neutral", 0.8),
          getLineWidth: 1,
          lineWidthUnits: "pixels",
          pickable: pick,
          onHover: (i) => this.hover(i.object ? spectrumHover("hf", i.object as LayerFeature) : null, i.x, i.y),
        }),
      );
    const sat = this.feats("satnogs").filter((f) => !!pointOf(f));
    if (sat.length)
      out.push(
        new ScatterplotLayer<LayerFeature>({
          id: "spec-satnogs",
          data: sat,
          getPosition: (d) => pointOf(d)!,
          getRadius: 5,
          radiusUnits: "pixels",
          stroked: true,
          getFillColor: (d) => (["bad", "failed"].includes(String(d.properties.status)) ? rgba("alert", 0.7) : rgba("notice", 0.35)),
          getLineColor: rgba("base", 0.6),
          getLineWidth: 1,
          lineWidthUnits: "pixels",
          pickable: pick,
          onHover: (i) => this.hover(i.object ? spectrumHover("satnogs", i.object as LayerFeature) : null, i.x, i.y),
        }),
      );
    return out;
  }
}
