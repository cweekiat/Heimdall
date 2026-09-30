// P3 map layers (task 023b): heat spots, outages, night lights, image footprints, next passes.
// Drawn with deck.gl on the same overlay in 2D and 3D. Nothing is ever fetched from an image host:
// the STAC link of an imagery footprint is shown as copyable text only. All text goes via textContent.
import { PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { api } from "./api";
import { h } from "./dom";
import { filters, onFilters } from "./filters";
import type { OpsMap } from "./map";
import { css, rgba } from "./theme/palette";
import { createWindow, EDGE_TOP, openWindow } from "./wm";
import {
  P3_LABEL,
  P3_LAYERS,
  P3_REFRESH_MS,
  TRUNCATED_NOTICE,
  dashSegments,
  hoverText,
  imageryLagDays,
  isRadar,
  lightsVsUsual,
  nextPass,
  parseLayer,
  pointOf,
  ringOf,
  thermalRadius,
} from "./p3model";
import type { LayerFC, LayerFeature, P3Layer } from "./p3model";

const EMPTY: LayerFC = { type: "FeatureCollection", features: [], truncated: false };
const NIGHT_HOURS = 168;

export class P3Layers {
  private data = new Map<string, LayerFC>(); // `${layer}|${aoi}`
  private tip: HTMLElement;
  private notice: HTMLElement;
  private chips: HTMLElement;
  private card: HTMLElement;
  private timers: number[] = [];
  private allOk: boolean | undefined; // does the server answer aoi=all? unknown until first try
  private rev = 0;
  private memo = new Map<P3Layer, { key: string; v: { aoi: string; f: LayerFeature }[] }>();
  private ctl = new Map<P3Layer, AbortController>();
  private seq = new Map<P3Layer, number>();
  /** Is there an open outage alert in this area? Then its outage ring is drawn in the alert colour. */
  hasOutageAlert: (aoi: string) => boolean = () => false;
  aois: () => string[] = () => [];

  constructor(private ops: OpsMap, wrap: HTMLElement) {
    this.tip = h("div", { class: "hovercard tip", hidden: true });
    this.notice = h("div", { class: "p3-notice", role: "status", hidden: true });
    this.chips = h("div", { class: "p3-chips" });
    this.card = createWindow({ id: "imagery", title: "Satellite image", icon: "notice", x: -60, y: EDGE_TOP + 40, w: 340, h: 260, minW: 260 });
    wrap.append(this.tip, this.notice);
    let last = filters.p3.join();
    onFilters(() => {
      if (filters.p3.join() === last) return;
      last = filters.p3.join();
      void this.refresh();
    });
  }

  /** The "next pass" chips, mounted in the Layers panel. */
  chipList(): HTMLElement {
    return this.chips;
  }

  start(): void {
    void this.refresh();
    for (const l of P3_LAYERS) this.timers.push(window.setInterval(() => void this.refresh([l]), P3_REFRESH_MS[l]));
  }

  private on(l: P3Layer): boolean {
    return filters.p3.includes(l);
  }

  /** One request per layer (`aoi=all`) when the server supports it; per-area requests otherwise. */
  async refresh(only: P3Layer[] = P3_LAYERS): Promise<void> {
    await Promise.all(only.filter((l) => this.on(l)).map((l) => this.refreshOne(l)));
    this.drawChrome();
    this.ops.render();
  }

  private async refreshOne(l: P3Layer): Promise<void> {
    this.ctl.get(l)?.abort();
    const ctl = new AbortController();
    this.ctl.set(l, ctl);
    const my = (this.seq.get(l) ?? 0) + 1;
    this.seq.set(l, my);
    const hours = l === "night_lights" ? NIGHT_HOURS : undefined;
    try {
      if (this.allOk !== false) {
        try {
          this.data.set(`${l}|*`, parseLayer(await api.layer(l, "all", hours, ctl.signal)));
          if (my !== this.seq.get(l)) return;
          this.allOk = true;
          this.rev++;
          return;
        } catch {
          if (my !== this.seq.get(l)) return;
          if (this.allOk === true) return; // a blip: keep the last picture
          this.allOk = false; // old server: fall back to one request per area
        }
      }
      await Promise.all(
        this.aois().map(async (a) => {
          try {
            const fc = parseLayer(await api.layer(l, a, hours, ctl.signal));
            if (my === this.seq.get(l)) this.data.set(`${l}|${a}`, fc);
          } catch {
            if (my === this.seq.get(l) && !this.data.has(`${l}|${a}`)) this.data.set(`${l}|${a}`, EMPTY);
          }
        }),
      );
      this.rev++;
    } finally {
      if (this.ctl.get(l) === ctl) this.ctl.delete(l);
    }
  }

  /** Features of a layer with the area each belongs to, memoised so deck sees the same array until data changes. */
  private of(l: P3Layer, keep: (f: LayerFeature) => boolean = () => true): { aoi: string; f: LayerFeature }[] {
    if (!this.on(l)) return [];
    const key = `${this.rev}|${this.aois().join()}`;
    const hit = this.memo.get(l);
    if (hit && hit.key === key) return hit.v;
    const src: { aoi: string; f: LayerFeature }[] =
      this.allOk === true
        ? (this.data.get(`${l}|*`) ?? EMPTY).features.map((f) => ({ aoi: String(f.properties.aoi ?? ""), f }))
        : this.aois().flatMap((aoi) => (this.data.get(`${l}|${aoi}`) ?? EMPTY).features.map((f) => ({ aoi, f })));
    const v = src.filter((x) => keep(x.f));
    this.memo.set(l, { key, v });
    return v;
  }

  private fcOf(l: P3Layer, aoi: string): LayerFC {
    if (this.allOk !== true) return this.data.get(`${l}|${aoi}`) ?? EMPTY;
    const all = this.data.get(`${l}|*`) ?? EMPTY;
    return { ...all, features: all.features.filter((f) => f.properties.aoi === aoi) };
  }

  private cut(l: P3Layer): boolean {
    return this.allOk === true ? !!this.data.get(`${l}|*`)?.truncated : this.aois().some((a) => this.data.get(`${l}|${a}`)?.truncated);
  }

  private drawChrome(): void {
    const cut = P3_LAYERS.filter((l) => this.on(l) && this.cut(l));
    this.notice.hidden = cut.length === 0;
    this.notice.textContent = cut.length ? `${cut.map((l) => P3_LABEL[l]).join(", ")}: ${TRUNCATED_NOTICE}` : "";
    const chips = this.on("overpass")
      ? this.aois().flatMap((a) => {
          const c = nextPass(a, this.fcOf("overpass", a));
          return c ? [h("span", { class: "chip p3-chip" }, `${c.aoi} ${c.text}`)] : [];
        })
      : [];
    this.chips.replaceChildren(...chips);
  }

  private hover(layer: P3Layer, f: LayerFeature | null, x: number, y: number): void {
    if (!f) {
      this.tip.hidden = true;
      return;
    }
    this.tip.hidden = false;
    this.tip.style.left = `${x + 12}px`;
    this.tip.style.top = `${y + 12}px`;
    this.tip.textContent = hoverText(layer, f);
  }

  layers(tool: string): unknown[] {
    const pick = tool === "none";
    const out: unknown[] = [];

    const lights = this.of("night_lights", (f) => typeof f.properties.h3_r4 === "string");
    if (lights.length)
      out.push(
        new H3HexagonLayer<{ aoi: string; f: LayerFeature }>({
          id: "p3-lights",
          data: lights,
          getHexagon: (d) => String(d.f.properties.h3_r4),
          extruded: false,
          filled: true,
          stroked: false,
          // brighter than usual = neutral, stronger; dimmer than usual = amber (something switched off)
          getFillColor: (d) => {
            const r = lightsVsUsual(d.f.properties);
            if (r === null) return rgba("neutral", 0.16);
            return r < 0.7 ? rgba("military", Math.min(0.7, 0.25 + (0.7 - r))) : rgba("neutral", Math.min(0.5, 0.12 + 0.2 * Math.min(r, 1.5)));
          },
          pickable: pick,
          onHover: (i) => this.hover("night_lights", i.object ? (i.object as { f: LayerFeature }).f : null, i.x, i.y),
        }),
      );

    const img = this.of("imagery", (f) => !!ringOf(f));
    if (img.length) {
      out.push(
        new PathLayer<number[][]>({
          id: "p3-imagery-s2",
          data: img.filter((x) => !isRadar(x.f.properties)).map((x) => ringOf(x.f)!),
          getPath: (d) => d as [number, number][],
          getColor: rgba("notice", 0.9),
          widthMinPixels: 1.5,
        }),
        new PathLayer<number[][]>({
          id: "p3-imagery-s1",
          data: img.filter((x) => isRadar(x.f.properties)).flatMap((x) => dashSegments(ringOf(x.f)!)),
          getPath: (d) => d as [number, number][],
          getColor: rgba("notice", 0.9),
          widthMinPixels: 1.5,
        }),
        // invisible centre points give footprints a hover and click target
        new ScatterplotLayer<{ aoi: string; f: LayerFeature }>({
          id: "p3-imagery-pick",
          data: img,
          getPosition: (d) => pointOf(d.f)!,
          getRadius: 9,
          radiusUnits: "pixels",
          getFillColor: rgba("notice", 0.25),
          pickable: pick,
          onHover: (i) => this.hover("imagery", i.object ? (i.object as { f: LayerFeature }).f : null, i.x, i.y),
          onClick: (i) => i.object && this.select((i.object as { f: LayerFeature }).f),
        }),
      );
    }

    const outage = this.of("outage", (f) => !!pointOf(f));
    if (outage.length)
      out.push(
        new ScatterplotLayer<{ aoi: string; f: LayerFeature }>({
          id: "p3-outage",
          data: outage,
          getPosition: (d) => pointOf(d.f)!,
          getRadius: 14,
          radiusUnits: "pixels",
          stroked: true,
          filled: false,
          getLineColor: (d) => (this.hasOutageAlert(d.aoi) ? rgba("alert", 0.95) : rgba("notice", 0.9)),
          getLineWidth: 3,
          lineWidthUnits: "pixels",
          pickable: pick,
          onHover: (i) => this.hover("outage", i.object ? (i.object as { f: LayerFeature }).f : null, i.x, i.y),
        }),
      );

    const heat = this.of("thermal", (f) => !!pointOf(f));
    if (heat.length)
      out.push(
        new ScatterplotLayer<{ aoi: string; f: LayerFeature }>({
          id: "p3-thermal",
          data: heat,
          getPosition: (d) => pointOf(d.f)!,
          getRadius: (d) => thermalRadius(d.f.properties.value),
          radiusUnits: "pixels",
          stroked: true,
          getFillColor: rgba("military", 0.55),
          getLineColor: rgba("military", 1),
          getLineWidth: 1,
          lineWidthUnits: "pixels",
          pickable: pick,
          onHover: (i) => this.hover("thermal", i.object ? (i.object as { f: LayerFeature }).f : null, i.x, i.y),
        }),
      );
    return out;
  }

  /** Show one footprint's details (also called from the alert panel). Turns the layer on if needed. */
  select(f: LayerFeature | string): void {
    let feat: LayerFeature | undefined = typeof f === "string" ? undefined : f;
    if (typeof f === "string") {
      feat = this.of("imagery").find((x) => x.f.id === f)?.f;
    }
    if (!feat) return;
    const p = feat.properties;
    const link = typeof p.url === "string" ? p.url : "";
    const lag = imageryLagDays(p);
    this.card.replaceChildren(
      h(
        "div",
        { class: "body" },
        h("h2", {}, String(p.platform ?? "Satellite image")),
        h("dl", { class: "kv" }, ...[
          ["Taken", hoverText("imagery", feat)],
          ["Kind", isRadar(p) ? "Radar (sees through cloud)" : "Optical (blocked by cloud)"],
          ["Age", lag === null ? "unknown" : `${lag} days`],
        ].flatMap(([k, v]) => [h("dt", {}, k!), h("dd", {}, v!)])),
        link
          ? h(
              "div",
              { class: "untrusted" },
              h("div", { class: "note" }, "Catalogue link (text only, never opened by Heimdall)"),
              h("pre", {}, link),
              h("button", { type: "button", onclick: () => void navigator.clipboard?.writeText(link).catch(() => undefined) }, "Copy link"),
            )
          : null,
      ),
    );
    openWindow("imagery");
  }

  /** Called by the alert panel: make sure the imagery layer is on, then show the footprint. */
  focus(id: string, setOn: () => void): void {
    if (!filters.p3.includes("imagery")) setOn();
    void this.refresh(["imagery"]).then(() => this.select(id));
  }
}

export const swatch = (l: P3Layer): string => (l === "outage" ? css("notice") : l === "thermal" ? css("military") : css("neutral"));
