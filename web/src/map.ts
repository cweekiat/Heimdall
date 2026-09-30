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
import { circleRing, dedupeTail, distNm, toLatLon } from "./aogeo";
import type { DisplayAo, LonLat } from "./aogeo";
import { css, currentMode, intensity, rgba, setMode, severityStyle, surfaceRgba } from "./theme/palette";

type RGBA = [number, number, number, number];

/** Share of affected traffic -> same red, stronger with more; almost none stays a faint neutral. */
export const ratioColor = (r: number): RGBA => (r < 0.02 ? rgba("neutral", 0.3) : rgba("alert", intensity(Math.min(1, r / 0.3))));

export interface MapState {
  alerts: Alert[];
  cells: Cell[];
  aois: Aoi[];
  aoAreas: DisplayAo[]; // outlines to draw (task 015)
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

export type DrawMode = "circle" | "polygon";
export type DrawResult =
  | { shape: "circle"; lat: number; lon: number; radius_nm: number }
  | { shape: "polygon"; polygon: [number, number][] };
export type DrawEvent = { kind: "progress"; text: string; ready: boolean } | { kind: "done"; result: DrawResult } | { kind: "cancel" };

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
    aoAreas: [],
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
  onDraw: (e: DrawEvent) => void = () => undefined;
  private draw: { mode: DrawMode; pts: LonLat[]; cursor: LonLat | null; dragging: boolean } | null = null;
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
    this.wireDraw();
    this.map.on("zoomend", () => this.render());
    this.map.on("moveend", () => this.onMove());
    this.map.on("click", (e) => {
      if (this.draw) return;
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

  // ---- drawing an area of operation: circle (press centre, drag radius) or polygon (click points, double-click to finish)
  private wireDraw(): void {
    const at = (e: { lngLat: { lng: number; lat: number } }): LonLat => [e.lngLat.lng, e.lngLat.lat];
    const down = (e: { lngLat: { lng: number; lat: number } }): void => {
      const d = this.draw;
      if (!d || d.mode !== "circle") return;
      d.pts = [at(e)];
      d.cursor = at(e);
      d.dragging = true;
      this.render();
    };
    const move = (e: { lngLat: { lng: number; lat: number } }): void => {
      const d = this.draw;
      if (!d) return;
      d.cursor = at(e);
      if (d.mode === "circle" && d.dragging) this.onDraw({ kind: "progress", text: `Radius ${distNm(d.pts[0]!, d.cursor).toFixed(1)} nm. Release to finish.`, ready: false });
      if (d.mode === "circle" ? d.dragging : d.pts.length > 0) this.render();
    };
    const up = (e: { lngLat: { lng: number; lat: number } }): void => {
      const d = this.draw;
      if (!d || d.mode !== "circle" || !d.dragging) return;
      d.dragging = false;
      const nm = Math.round(distNm(d.pts[0]!, at(e)) * 10) / 10;
      if (nm < 1) {
        d.pts = [];
        this.onDraw({ kind: "progress", text: "Press on the centre, then drag outwards.", ready: false });
        return this.render();
      }
      const [lat, lon] = toLatLon([d.pts[0]!])[0]!;
      this.endDraw();
      this.onDraw({ kind: "done", result: { shape: "circle", lat, lon, radius_nm: nm } });
    };
    this.map.on("mousedown", down);
    this.map.on("touchstart", down);
    this.map.on("mousemove", move);
    this.map.on("touchmove", move);
    this.map.on("mouseup", up);
    this.map.on("touchend", (e) => {
      const d = this.draw;
      if (d?.mode === "circle" && d.cursor) up({ lngLat: { lng: d.cursor[0], lat: d.cursor[1] } });
      else up(e);
    });
    this.map.on("click", (e) => {
      const d = this.draw;
      if (!d || d.mode !== "polygon") return;
      d.pts.push(at(e));
      this.polygonProgress();
      this.render();
    });
    this.map.on("dblclick", (e) => {
      if (this.draw?.mode !== "polygon") return;
      e.preventDefault();
      this.finishPolygon();
    });
  }

  private polygonProgress(): void {
    const n = this.draw?.pts.length ?? 0;
    this.onDraw({ kind: "progress", text: n < 3 ? `${n} point(s). Click at least 3.` : `${n} points. Double-click, or press Finish, to close the area.`, ready: n >= 3 });
  }

  startDraw(mode: DrawMode): void {
    this.setTool("none");
    this.draw = { mode, pts: [], cursor: null, dragging: false };
    this.map.getCanvas().style.cursor = "crosshair";
    this.map.doubleClickZoom.disable();
    if (mode === "circle") this.map.dragPan.disable();
    this.onDraw({ kind: "progress", text: mode === "circle" ? "Press on the centre, then drag outwards." : "Click the corners of the area.", ready: false });
    this.render();
  }

  private endDraw(): void {
    this.draw = null;
    this.map.getCanvas().style.cursor = "";
    this.map.doubleClickZoom.enable();
    this.map.dragPan.enable();
    this.render();
  }

  cancelDraw(): void {
    if (!this.draw) return;
    this.endDraw();
    this.onDraw({ kind: "cancel" });
  }

  undoPoint(): void {
    if (this.draw?.mode !== "polygon" || !this.draw.pts.length) return;
    this.draw.pts.pop();
    this.polygonProgress();
    this.render();
  }

  finishPolygon(): void {
    const d = this.draw;
    if (d?.mode !== "polygon") return;
    const pts = dedupeTail(d.pts);
    if (pts.length < 3) {
      d.pts = pts;
      this.polygonProgress();
      return this.render();
    }
    this.endDraw();
    this.onDraw({ kind: "done", result: { shape: "polygon", polygon: toLatLon(pts) } });
  }

  isDrawing(): boolean {
    return this.draw !== null;
  }

  private draftLayers(): unknown[] {
    const d = this.draw;
    if (!d || !d.pts.length) return [];
    let path: LonLat[] = [];
    if (d.mode === "circle") {
      if (d.dragging && d.cursor) path = circleRing(d.pts[0]![1], d.pts[0]![0], Math.max(0.5, distNm(d.pts[0]!, d.cursor)));
    } else {
      path = [...d.pts, ...(d.cursor ? [d.cursor] : []), d.pts[0]!];
    }
    return [
      new PathLayer<{ p: LonLat[] }>({
        id: "ao-draft",
        data: [{ p: path }],
        getPath: (x) => x.p,
        getColor: rgba("base", 0.95),
        widthMinPixels: 2,
      }),
      new ScatterplotLayer<LonLat>({
        id: "ao-draft-pts",
        data: d.pts,
        getPosition: (p) => p,
        getRadius: 5,
        radiusUnits: "pixels",
        getFillColor: rgba("base", 1),
        getLineColor: rgba("military", 1),
        stroked: true,
        getLineWidth: 2,
        lineWidthUnits: "pixels",
      }),
    ];
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
          features: s.aoAreas.map((a) => ({
            type: "Feature",
            properties: { role: a.role, active: a.active },
            geometry: { type: "Polygon", coordinates: [a.ring] },
          })),
        } as never,
        stroked: true,
        filled: false,
        getLineColor: (f: { properties: { role: string; active: boolean } }) =>
          f.properties.role === "control" ? rgba("notice", f.properties.active ? 0.6 : 0.3) : rgba("neutral", f.properties.active ? 0.7 : 0.35),
        getLineWidth: 2,
        lineWidthUnits: "pixels",
      }),
      new GeoJsonLayer({
        id: "aoi-selected",
        data: {
          type: "FeatureCollection",
          features: s.aoAreas.filter((a) => a.selected).map((a) => ({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [a.ring] } })),
        } as never,
        stroked: true,
        filled: false,
        getLineColor: rgba("military", 0.95),
        getLineWidth: 4,
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
      ...(this.draftLayers() as never[]),
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
          data: [{ p: circleRing(s.points[0]![1], s.points[0]![0], haversineNm(s.points[0]!, s.points[1]!)) }],
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
    sw(`background:transparent;outline:2px solid ${css("neutral")}`, "Thin outline: a monitored area. Draw your own with the pen icon (amber = selected)"),
    sw(`background:${css("notice", 0.35)};outline:1px solid ${css("notice")}`, "Grey box: too little data to judge (no data is not no activity)"),
  );
}
