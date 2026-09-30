import { GeoJsonLayer, PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import * as maplibregl from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { BaseLayers } from "./basemap";
import type { Provider } from "./basemap";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Alert, Aoi, Cell, Evidence, TrackPt } from "./api";
import { h } from "./dom";
import { kindLabel } from "./labels";
import { haversineNm } from "./geoutil";

type RGBA = [number, number, number, number];

export const KIND_COLOR: Record<string, RGBA> = {
  gnss_jamming: [248, 81, 73, 255],
  gnss_spoofing: [219, 109, 255, 255],
  mil_orbit: [88, 166, 255, 255],
  ais_spoofing: [63, 185, 200, 255],
  ais_gap: [120, 200, 120, 255],
  thermal: [255, 140, 0, 255],
  outage: [200, 200, 200, 255],
  event_surge: [255, 211, 61, 255],
  warning_area: [255, 160, 200, 255],
  composite: [255, 255, 255, 255],
};
const GREEN: RGBA = [63, 185, 80, 170];
const AMBER: RGBA = [210, 153, 34, 190];
const RED: RGBA = [248, 81, 73, 210];

export const ratioColor = (r: number): RGBA => (r > 0.1 ? RED : r >= 0.02 ? AMBER : GREEN);

function circle(lat: number, lon: number, nm: number): number[][] {
  const out: number[][] = [];
  const r = (nm * 1.852) / 6371.0088;
  const la = (lat * Math.PI) / 180;
  const lo = (lon * Math.PI) / 180;
  for (let i = 0; i <= 90; i++) {
    const b = (i / 90) * 2 * Math.PI;
    const p = Math.asin(Math.sin(la) * Math.cos(r) + Math.cos(la) * Math.sin(r) * Math.cos(b));
    const l = lo + Math.atan2(Math.sin(b) * Math.sin(r) * Math.cos(la), Math.cos(r) - Math.sin(la) * Math.sin(p));
    out.push([(l * 180) / Math.PI, (p * 180) / Math.PI]);
  }
  return out;
}

export interface MapState {
  alerts: Alert[];
  cells: Cell[];
  aois: Aoi[];
  tracks: { hex: string; path: TrackPt[] }[];
  evidence: Evidence[];
  selected: string | null;
  showCoverage: boolean;
  hoursAgo: number; // time slider: 0 = now, 24 = 24 h ago
  asOf: number | null; // global time window end (ms); overrides the slider when set
  tool: "none" | "measure" | "ring";
  points: [number, number][]; // measure polyline / ring centre (+ radius point)
}

export interface Cluster {
  lon: number;
  lat: number;
  alerts: Alert[];
}

/** Grid clustering used below zoom 5 so dense areas stay readable (declutter). */
export function cluster(alerts: Alert[], zoom: number): Cluster[] {
  if (zoom >= 5) return alerts.map((a) => ({ lon: a.lon as number, lat: a.lat as number, alerts: [a] }));
  const cell = zoom < 3.5 ? 4 : 2;
  const m = new Map<string, Cluster>();
  for (const a of alerts) {
    const k = `${Math.floor((a.lon as number) / cell)}:${Math.floor((a.lat as number) / cell)}`;
    const c = m.get(k);
    if (c) c.alerts.push(a);
    else m.set(k, { lon: a.lon as number, lat: a.lat as number, alerts: [a] });
  }
  return [...m.values()];
}

// maplibre-gl v6 ships its worker as separate ES modules that Vite does not emit by itself;
// bundle it (with its shared chunk) and point MapLibre at it.
maplibregl.setWorkerUrl(workerUrl);

export class OpsMap {
  private map: maplibregl.Map;
  readonly layers: BaseLayers;
  private overlay = new MapboxOverlay({ interleaved: false, layers: [] });
  state: MapState = {
    alerts: [],
    cells: [],
    aois: [],
    tracks: [],
    evidence: [],
    selected: null,
    showCoverage: false,
    hoursAgo: 0,
    asOf: null,
    tool: "none",
    points: [],
  };
  onCursor: (lon: number, lat: number, zoom: number) => void = () => undefined;
  onHover: (a: Alert[] | null, x: number, y: number) => void = () => undefined;
  onTool: () => void = () => undefined;
  /** Fires (debounced by the caller) when the view changes; used to refetch visible traffic. */
  onMove: () => void = () => undefined;
  /** Extra deck layers (traffic, notices) drawn above the alert cells and below alert markers. */
  extraLayers: (tool: MapState["tool"], zoom: number) => unknown[] = () => [];

  constructor(
    container: HTMLElement,
    theme: "dark" | "light",
    provider: Provider,
    overlays: string[],
    private onPick: (id: string) => void,
  ) {
    this.map = new maplibregl.Map({
      container,
      style: { version: 8, sources: {}, layers: [] },
      center: [108, 14],
      zoom: 3.2,
      attributionControl: { compact: true },
    });
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    this.map.addControl(new maplibregl.ScaleControl({ unit: "nautical", maxWidth: 120 }), "bottom-left");
    this.map.on("mousemove", (e) => this.onCursor(e.lngLat.lng, e.lngLat.lat, this.map.getZoom()));
    this.map.on("zoomend", () => this.render());
    this.map.on("moveend", () => this.onMove());
    this.map.on("click", (e) => {
      if (this.state.tool === "none") return;
      const p: [number, number] = [e.lngLat.lng, e.lngLat.lat];
      this.state.points = this.state.tool === "ring" ? [...this.state.points.slice(0, 1), p].slice(-2) : [...this.state.points, p];
      if (this.state.tool === "ring" && this.state.points.length === 1) this.state.points = [p];
      this.onTool();
      this.render();
    });
    this.map.addControl(this.overlay as unknown as maplibregl.IControl);
    this.layers = new BaseLayers(this.map, provider, overlays, theme);
    this.map.setStyle(this.layers.initialStyle(), { diff: false });
    new ResizeObserver(() => this.map.resize()).observe(container);
  }

  setTool(t: MapState["tool"]): void {
    this.state.tool = t;
    this.state.points = [];
    this.map.getCanvas().style.cursor = t === "none" ? "" : "crosshair";
    this.onTool();
    this.render();
  }

  /** Distance text for the active tool, in nautical miles. */
  toolReadout(): string {
    const p = this.state.points;
    if (this.state.tool === "measure" && p.length > 1) {
      let d = 0;
      for (let i = 1; i < p.length; i++) d += haversineNm(p[i - 1]!, p[i]!);
      return `Measure: ${d.toFixed(1)} nm (${(d * 1.852).toFixed(1)} km), ${p.length - 1} leg(s)`;
    }
    if (this.state.tool === "ring" && p.length > 1) return `Range ring: radius ${haversineNm(p[0]!, p[1]!).toFixed(1)} nm`;
    if (this.state.tool !== "none") return this.state.tool === "ring" ? "Range ring: click centre, then radius point" : "Measure: click points";
    return "";
  }

  zoomBy(d: number): void {
    this.map.easeTo({ zoom: this.map.getZoom() + d, duration: 200 });
  }

  /** Visible box as "w,s,e,n" (w > e means the view crosses the antimeridian). */
  bbox(): string {
    const b = this.map.getBounds();
    const wrap = (x: number): number => ((((x + 180) % 360) + 360) % 360) - 180;
    const r = (x: number): string => x.toFixed(3);
    const span = b.getEast() - b.getWest();
    if (span >= 360) return `-180,${r(Math.max(-90, b.getSouth()))},180,${r(Math.min(90, b.getNorth()))}`;
    return `${r(wrap(b.getWest()))},${r(Math.max(-90, b.getSouth()))},${r(wrap(b.getEast()))},${r(Math.min(90, b.getNorth()))}`;
  }

  /** Screen position of a point (px inside the map). */
  project(lon: number, lat: number): { x: number; y: number } {
    const p = this.map.project([lon, lat]);
    return { x: p.x, y: p.y };
  }

  /** Pan (keep zoom) so follow mode keeps a moving target in view. */
  panTo(lon: number, lat: number): void {
    this.map.easeTo({ center: [lon, lat], duration: 400 });
  }

  zoom(): number {
    return this.map.getZoom();
  }

  zoomTo(lon: number, lat: number): void {
    this.map.flyTo({ center: [lon, lat], zoom: this.map.getZoom() + 2 });
  }

  setTheme(theme: "dark" | "light"): void {
    this.layers.setTheme(theme);
  }

  flyTo(lon: number, lat: number): void {
    this.map.flyTo({ center: [lon, lat], zoom: Math.max(this.map.getZoom(), 5.5) });
  }

  private asOf(): number {
    return this.state.asOf ?? Date.now() - this.state.hoursAgo * 3600_000;
  }

  render(): void {
    const s = this.state;
    const t = this.asOf();
    const alerts = s.alerts.filter((a) => new Date(a.first_seen).getTime() <= t && a.lat !== null);
    const zoomKey = this.map.getZoom() < 5 ? 1 : 0;
    const clusters = cluster(alerts, this.map.getZoom());
    const cells = s.cells.filter((c) => new Date(c.ts).getTime() <= t);
    const ratioCells = cells.filter((c) => c.ratio !== null);
    const layers = [
      new GeoJsonLayer({
        id: "aoi",
        data: {
          type: "FeatureCollection",
          features: s.aois.map((a) => ({
            type: "Feature",
            properties: { name: a.name, role: a.role },
            geometry: { type: "Polygon", coordinates: [circle(a.lat, a.lon, a.radius_nm)] },
          })),
        } as never,
        stroked: true,
        filled: false,
        getLineColor: (f: { properties: { role: string } }) =>
          f.properties.role === "control"
            ? [160, 160, 160, 160]
            : [88, 166, 255, 160],
        getLineWidth: 2,
        lineWidthUnits: "pixels",
      }),
      s.showCoverage &&
        new H3HexagonLayer<Cell>({
          id: "coverage",
          data: cells,
          getHexagon: (d) => d.h3,
          getFillColor: (d) => [120, 140, 255, Math.min(200, 30 + d.n_quality * 12)],
          getElevation: 0,
          extruded: false,
          pickable: false,
        }),
      s.showCoverage &&
        new H3HexagonLayer<Cell>({
          id: "nodata",
          data: cells.filter((c) => c.n_quality < 3),
          getHexagon: (d) => d.h3,
          getFillColor: [150, 150, 150, 90],
          getLineColor: [200, 200, 200, 200],
          lineWidthMinPixels: 1,
          stroked: true,
          extruded: false,
          pickable: false,
        }),
      new H3HexagonLayer<Cell>({
        id: "cells",
        data: ratioCells,
        getHexagon: (d) => d.h3,
        getFillColor: (d) => ratioColor(d.ratio ?? 0),
        getLineColor: [255, 255, 255, 90],
        lineWidthMinPixels: 1,
        stroked: true,
        extruded: false,
        pickable: true,
      }),
      new GeoJsonLayer({
        id: "ellipses",
        data: s.evidence.filter((e) => e.geometry).map((e) => ({ type: "Feature", properties: {}, geometry: e.geometry })) as never,
        stroked: true,
        filled: true,
        getFillColor: [248, 81, 73, 50],
        getLineColor: [248, 81, 73, 255],
        lineWidthMinPixels: 2,
      }),
      ...(this.extraLayers(s.tool, this.map.getZoom()) as never[]),
      new PathLayer<{ hex: string; path: TrackPt[] }>({
        id: "tracks",
        data: s.tracks,
        getPath: (d) => d.path.map((p) => [p.lon, p.lat] as [number, number]),
        getColor: [88, 166, 255, 255],
        widthMinPixels: 3,
      }),
      new ScatterplotLayer<Cluster>({
        id: "halo",
        data: clusters.filter((c) => c.alerts.some((x) => x.id === s.selected)),
        getPosition: (c) => [c.lon, c.lat],
        getRadius: 22,
        radiusUnits: "pixels",
        stroked: true,
        filled: false,
        getLineColor: [255, 211, 61, 220],
        getLineWidth: 3,
        lineWidthUnits: "pixels",
      }),
      new ScatterplotLayer<Cluster>({
        id: "alerts",
        data: clusters,
        getPosition: (c) => [c.lon, c.lat],
        getFillColor: (c) => (c.alerts.length > 1 ? [110, 118, 129, 235] : (KIND_COLOR[c.alerts[0]!.kind] ?? [255, 255, 255, 255])),
        getLineColor: (c) => (c.alerts.some((a) => a.id === s.selected) ? [255, 211, 61, 255] : [0, 0, 0, 255]),
        getLineWidth: (c) => (c.alerts.some((a) => a.id === s.selected) ? 4 : 1.5),
        getRadius: (c) => (c.alerts.length > 1 ? 14 : c.alerts[0]!.id === s.selected ? 13 : 9),
        radiusUnits: "pixels",
        lineWidthUnits: "pixels",
        stroked: true,
        pickable: s.tool === "none",
        updateTriggers: { getLineColor: s.selected, getLineWidth: s.selected, getRadius: s.selected, getFillColor: zoomKey },
        onHover: (info) => {
          const c = info.object as Cluster | undefined;
          this.onHover(c ? c.alerts : null, info.x, info.y);
        },
        onClick: (info) => {
          const c = info.object as Cluster | undefined;
          if (!c) return;
          if (c.alerts.length > 1) this.zoomTo(c.lon, c.lat);
          else this.onPick(c.alerts[0]!.id);
        },
      }),
      new TextLayer<Cluster>({
        id: "cluster-n",
        data: clusters.filter((c) => c.alerts.length > 1),
        getPosition: (c) => [c.lon, c.lat],
        getText: (c) => String(c.alerts.length),
        getSize: 13,
        getColor: [255, 255, 255, 255],
        fontWeight: 700,
      }),
      s.points.length > 0 &&
        new ScatterplotLayer<[number, number]>({
          id: "tool-pts",
          data: s.points,
          getPosition: (d) => d,
          getRadius: 5,
          radiusUnits: "pixels",
          getFillColor: [255, 211, 61, 255],
        }),
      s.tool === "measure" &&
        s.points.length > 1 &&
        new PathLayer<{ p: [number, number][] }>({
          id: "measure",
          data: [{ p: s.points }],
          getPath: (d) => d.p,
          getColor: [255, 211, 61, 255],
          widthMinPixels: 2,
        }),
      s.tool === "ring" &&
        s.points.length > 1 &&
        new PathLayer<{ p: number[][] }>({
          id: "ring",
          data: [{ p: circle(s.points[0]![1], s.points[0]![0], haversineNm(s.points[0]!, s.points[1]!)) }],
          getPath: (d) => d.p as [number, number][],
          getColor: [255, 211, 61, 255],
          widthMinPixels: 2,
        }),
    ];
    this.overlay.setProps({ layers: layers.filter(Boolean) as never[] });
  }
}

export function legend(): HTMLElement {
  const sw = (c: RGBA | number[], label: string) =>
    h("span", {}, h("i", { class: "sw", style: `background:rgb(${c[0]},${c[1]},${c[2]})` }), label);
  return h(
    "div",
    {},
    h("div", {}, "Aircraft affected", sw(GREEN, "<2%"), sw(AMBER, "2-10%"), sw(RED, ">10%")),
    h(
      "div",
      {},
      "Alerts",
      ...["gnss_jamming", "gnss_spoofing", "mil_orbit", "composite"].map((k) => sw(KIND_COLOR[k]!, kindLabel(k))),
    ),
    h("div", {}, "Coverage", h("i", { class: "sw", style: "background:rgba(150,150,150,.5);outline:1px solid #ccc" }), "grey outline = too little data (no data is not no activity)"),
  );
}
