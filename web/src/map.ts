import { GeoJsonLayer, PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import * as maplibregl from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { Protocol } from "pmtiles";
import { BaseLayers } from "./basemap";
import { Throttle, probeBasemap, setViewMode, unavailableHint, view } from "./map3d";
import type { ViewMode } from "./map3d";
import type { Provider } from "./basemap";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Alert, Aoi, Cell, Evidence, TrackPt } from "./api";
import { h } from "./dom";
import { haversineNm } from "./geoutil";
import { css, currentMode, intensity, rgba, setMode, severityStyle, surfaceRgba } from "./theme/palette";

type RGBA = [number, number, number, number];

/** Share of affected traffic -> same red, stronger with more; almost none stays a faint neutral. */
export const ratioColor = (r: number): RGBA => (r < 0.02 ? rgba("neutral", 0.3) : rgba("alert", intensity(Math.min(1, r / 0.3))));

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

/** Cluster marker opacity: more or higher-priority alerts look stronger, colour stays red. */
function clusterOpacity(c: { alerts: Alert[] }): number {
  return c.alerts.length > 1 ? 0.95 : severityStyle(c.alerts[0]!.severity).opacity;
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
// Offline basemap: pmtiles:// URLs are served by HTTP range requests to /basemap/world.pmtiles.
maplibregl.addProtocol("pmtiles", new Protocol().tile);

export class OpsMap {
  private map: maplibregl.Map;
  readonly layers: BaseLayers;
  private container!: HTMLElement;
  private hintEl: HTMLElement | null = null;
  private soon = new Throttle(() => this.render(), 2);
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
    this.layers.onBaseChange = (id) => void this.checkOffline(id === "offline");
    this.layers.applyInitial();
    this.map.dragRotate.disable();
    this.map.touchZoomRotate.disableRotation();
    this.map.setMaxPitch(85);
    this.container = container;
    new ResizeObserver(() => this.map.resize()).observe(container);
  }

  /** Throttled render (max 2 Hz, coalesced on animation frames) for traffic updates. */
  renderSoon(): void {
    this.soon.poke();
  }

  viewMode(): ViewMode {
    return view.mode;
  }

  setView(mode: ViewMode): void {
    view.mode = mode;
    setViewMode(this.map as never, mode);
    this.render();
  }

  resetNorth(): void {
    this.map.easeTo({ bearing: 0, pitch: view.mode === "3d" ? 60 : 0, duration: 300 });
  }

  /** Non-blocking hint when the offline map files are missing; the background stays plain. */
  private async checkOffline(offline: boolean): Promise<void> {
    this.hintEl?.remove();
    this.hintEl = null;
    if (!offline) return;
    const st = await probeBasemap((u) => fetch(u));
    if (st.available) return;
    this.hintEl = unavailableHint(st.hint, document) as unknown as HTMLElement;
    this.container.append(this.hintEl);
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
    setMode(theme);
    this.layers.setTheme(theme);
    this.render();
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
        getLineColor: (f: { properties: { role: string } }) => (f.properties.role === "control" ? rgba("notice", 0.6) : rgba("neutral", 0.7)),
        getLineWidth: 2,
        lineWidthUnits: "pixels",
      }),
      s.showCoverage &&
        new H3HexagonLayer<Cell>({
          id: "coverage",
          data: cells,
          getHexagon: (d) => d.h3,
          getFillColor: (d) => rgba("neutral", Math.min(0.5, 0.06 + d.n_quality * 0.02)),
          getElevation: 0,
          extruded: false,
          pickable: false,
        }),
      s.showCoverage &&
        new H3HexagonLayer<Cell>({
          id: "nodata",
          data: cells.filter((c) => c.n_quality < 3),
          getHexagon: (d) => d.h3,
          getFillColor: rgba("notice", 0.3),
          getLineColor: rgba("notice", 0.8),
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
        getLineColor: rgba("base", 0.35),
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
        getFillColor: rgba("alert", 0.2),
        getLineColor: rgba("alert", 1),
        lineWidthMinPixels: 2,
      }),
      ...(this.extraLayers(s.tool, this.map.getZoom()) as never[]),
      new PathLayer<{ hex: string; path: TrackPt[] }>({
        id: "tracks",
        data: s.tracks,
        getPath: (d) => d.path.map((p) => [p.lon, p.lat] as [number, number]),
        getColor: rgba("neutral", 1),
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
        getLineColor: rgba("military", 0.9),
        getLineWidth: 3,
        lineWidthUnits: "pixels",
      }),
      new ScatterplotLayer<Cluster>({
        id: "alerts",
        data: clusters,
        getPosition: (c) => [c.lon, c.lat],
        getFillColor: (c) => rgba("alert", clusterOpacity(c)),
        getLineColor: (c) => (c.alerts.some((a) => a.id === s.selected) ? rgba("military", 1) : surfaceRgba(1)),
        getLineWidth: (c) => (c.alerts.some((a) => a.id === s.selected) ? 4 : 1.5),
        getRadius: (c) => (c.alerts.length > 1 ? 14 : severityStyle(c.alerts[0]!.severity).radius + (c.alerts[0]!.id === s.selected ? 3 : 0)),
        radiusUnits: "pixels",
        lineWidthUnits: "pixels",
        stroked: true,
        pickable: s.tool === "none",
        updateTriggers: { getLineColor: [s.selected, currentMode()], getLineWidth: s.selected, getRadius: s.selected, getFillColor: [zoomKey, currentMode()] },
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
        getColor: rgba("base", 1),
        fontWeight: 700,
      }),
      s.points.length > 0 &&
        new ScatterplotLayer<[number, number]>({
          id: "tool-pts",
          data: s.points,
          getPosition: (d) => d,
          getRadius: 5,
          radiusUnits: "pixels",
          getFillColor: rgba("military", 1),
        }),
      s.tool === "measure" &&
        s.points.length > 1 &&
        new PathLayer<{ p: [number, number][] }>({
          id: "measure",
          data: [{ p: s.points }],
          getPath: (d) => d.p,
          getColor: rgba("military", 1),
          widthMinPixels: 2,
        }),
      s.tool === "ring" &&
        s.points.length > 1 &&
        new PathLayer<{ p: number[][] }>({
          id: "ring",
          data: [{ p: circle(s.points[0]![1], s.points[0]![0], haversineNm(s.points[0]!, s.points[1]!)) }],
          getPath: (d) => d.p as [number, number][],
          getColor: rgba("military", 1),
          widthMinPixels: 2,
        }),
    ];
    this.overlay.setProps({ layers: layers.filter(Boolean) as never[] });
  }
}

export function legend(): HTMLElement {
  const sw = (style: string, label: string) => h("div", { class: "lg" }, h("i", { class: "sw", style }), label);
  const dot = (c: string) => `background:${c};border-radius:50%`;
  return h(
    "div",
    { class: "legend-body" },
    sw(dot(css("alert")), "Red: needs attention (an alert). Bigger and stronger = higher priority"),
    sw(`background:${css("alert", 0.25)};outline:1px solid ${css("alert")}`, "Red area: share of aircraft with bad GPS (stronger = more)"),
    sw(dot(css("neutral")), "Grey-blue: normal aircraft, ships and tracks (aircraft are planes, ships are arrows)"),
    sw(`background:${css("military")};transform:rotate(45deg)`, "Amber diamond: military aircraft. Amber ring: selected"),
    sw(`background:transparent;outline:1px dashed ${css("notice")}`, "Grey outline: navigation warning or NOTAM area"),
    sw(`background:${css("notice", 0.35)};outline:1px solid ${css("notice")}`, "Grey box: too little data to judge (no data is not no activity)"),
  );
}
