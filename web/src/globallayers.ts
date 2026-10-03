// Global context layers (task 049): military bases (OpenStreetMap) and conflict events (GDELT, machine-coded news).
// Context only: neither raises alerts. Conflict events are noisy, so by default only events reported by 2+ sources
// are drawn, sized by source count. Colours avoid alert red, satellite cyan and the military role colours.
import { ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { api } from "./api";
import type { BaseProps } from "./api";
import { h } from "./dom";
import { filters, onFilters } from "./filters";
import type { OpsMap } from "./map";
import { parseLayer } from "./p3model";
import type { LayerFeature } from "./p3model";
import { conflictRadius, conflictText } from "./contextmodel";

export type ContextLayer = "bases" | "conflict";
const BASE_RGB: [number, number, number] = [45, 58, 74]; // dark slate outline + white fill: readable on light and dark maps
const CONFLICT_RGB: [number, number, number] = [255, 132, 72];

interface Base {
  p: [number, number];
  b: BaseProps;
}

export class GlobalLayers {
  private bases: Base[] = [];
  private conflict: LayerFeature[] = [];
  private tip: HTMLElement;
  private basesAt = 0;
  minSources = 2;

  constructor(
    private ops: OpsMap,
    wrap: HTMLElement,
  ) {
    this.tip = h("div", { class: "hovercard tip", hidden: true });
    wrap.append(this.tip);
    // 055: refetch only when the context layers themselves change, not on every filter tweak
    let last = filters.context.join(",");
    onFilters(() => {
      const now = filters.context.join(",");
      if (now !== last) {
        last = now;
        void this.refresh();
      }
    });
  }

  start(): void {
    void this.refresh();
    window.setInterval(() => void this.refresh(), 300_000);
  }

  private on(l: ContextLayer): boolean {
    return filters.context.includes(l);
  }

  async refresh(): Promise<void> {
    try {
      if (this.on("bases") && Date.now() - this.basesAt > 86_400_000) {
        const fc = await api.bases();
        this.bases = fc.features.map((f) => ({ p: f.geometry.coordinates, b: f.properties }));
        this.basesAt = Date.now();
      }
      if (this.on("conflict")) this.conflict = parseLayer(await api.layer("conflict", "all", 24)).features;
    } catch {
      /* context layers are optional: keep what we have */
    }
    this.ops.render();
  }

  status(l: ContextLayer): string {
    if (l === "bases") return this.bases.length ? `${this.bases.length.toLocaleString("en")}` : "";
    const n = this.conflict.filter((f) => Number(f.properties.sources ?? 0) >= this.minSources).length;
    return this.conflict.length ? `${n} in 24 h` : "";
  }

  private hover(text: string | null, x: number, y: number): void {
    this.tip.hidden = !text;
    if (!text) return;
    this.tip.textContent = text;
    this.tip.style.left = `${x + 12}px`;
    this.tip.style.top = `${y + 12}px`;
  }

  layers(zoom: number): unknown[] {
    const out: unknown[] = [];
    if (this.on("bases") && this.bases.length && zoom >= 2.5) {
      out.push(
        new ScatterplotLayer<Base>({
          id: "ctx-bases",
          data: this.bases,
          getPosition: (d) => d.p,
          getFillColor: [255, 255, 255, 220],
          getLineColor: [...BASE_RGB, 255],
          stroked: true,
          lineWidthMinPixels: 1.5,
          radiusMinPixels: zoom >= 6 ? 6 : 3,
          getRadius: 1,
          pickable: true,
          onHover: (i) => this.hover(i.object ? [(i.object as Base).b.name, (i.object as Base).b.label, (i.object as Base).b.operator, (i.object as Base).b.icao, "© OpenStreetMap contributors"].filter(Boolean).join(" · ") : null, i.x, i.y),
        }),
      );
      if (zoom >= 6) {
        out.push(
          new TextLayer<Base>({
            id: "ctx-bases-label",
            data: this.bases,
            getPosition: (d) => d.p,
            getText: (d) => d.b.name,
            getSize: 11,
            getPixelOffset: [0, 14],
            getColor: [255, 255, 255, 240],
            outlineWidth: 2,
            outlineColor: [...BASE_RGB, 230],
            fontSettings: { sdf: true },
          }),
        );
      }
    }
    if (this.on("conflict")) {
      const data = this.conflict.filter((f) => Number(f.properties.sources ?? 0) >= this.minSources && f.geometry?.type === "Point");
      out.push(
        new ScatterplotLayer<LayerFeature>({
          id: "ctx-conflict",
          data,
          getPosition: (f) => (f.geometry!.coordinates as [number, number]),
          getFillColor: [...CONFLICT_RGB, 70],
          getLineColor: [...CONFLICT_RGB, 255],
          stroked: true,
          lineWidthMinPixels: 1.5,
          radiusUnits: "pixels",
          getRadius: (f) => conflictRadius(Number(f.properties.sources ?? 1)),
          pickable: true,
          onHover: (i) => this.hover(i.object ? conflictText((i.object as LayerFeature).properties) : null, i.x, i.y),
          onClick: (i) => {
            const url = (i.object as LayerFeature | undefined)?.properties.url;
            if (typeof url === "string" && /^https?:\/\//.test(url)) window.open(url, "_blank", "noopener,noreferrer");
          },
        }),
      );
    }
    return out;
  }
}
