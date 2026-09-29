import { GeoJsonLayer, PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { MapboxOverlay } from "@deck.gl/mapbox";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Alert, Aoi, Cell, Evidence, TrackPt } from "./api";
import { h } from "./dom";

type RGBA = [number, number, number, number];

// Free vector styles (CARTO basemaps, no key). Attribution is shown by MapLibre.
const STYLES = {
  dark: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
};

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
}

export class OpsMap {
  private map: maplibregl.Map;
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
  };

  constructor(
    container: HTMLElement,
    theme: "dark" | "light",
    private onPick: (id: string) => void,
  ) {
    this.map = new maplibregl.Map({
      container,
      style: STYLES[theme],
      center: [108, 14],
      zoom: 3.2,
      attributionControl: { compact: true },
    });
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    this.map.addControl(this.overlay as unknown as maplibregl.IControl);
    new ResizeObserver(() => this.map.resize()).observe(container);
  }

  setTheme(theme: "dark" | "light"): void {
    this.map.setStyle(STYLES[theme]);
  }

  flyTo(lon: number, lat: number): void {
    this.map.flyTo({ center: [lon, lat], zoom: Math.max(this.map.getZoom(), 5.5) });
  }

  private asOf(): number {
    return Date.now() - this.state.hoursAgo * 3600_000;
  }

  render(): void {
    const s = this.state;
    const t = this.asOf();
    const alerts = s.alerts.filter((a) => new Date(a.first_seen).getTime() <= t && a.lat !== null);
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
      new PathLayer<{ hex: string; path: TrackPt[] }>({
        id: "tracks",
        data: s.tracks,
        getPath: (d) => d.path.map((p) => [p.lon, p.lat] as [number, number]),
        getColor: [88, 166, 255, 255],
        widthMinPixels: 3,
      }),
      new ScatterplotLayer<Alert>({
        id: "alerts",
        data: alerts,
        getPosition: (a) => [a.lon as number, a.lat as number],
        getFillColor: (a) => KIND_COLOR[a.kind] ?? [255, 255, 255, 255],
        getLineColor: (a) => (a.id === s.selected ? [255, 211, 61, 255] : [0, 0, 0, 255]),
        getLineWidth: (a) => (a.id === s.selected ? 4 : 1.5),
        getRadius: (a) => (a.id === s.selected ? 13 : 9),
        radiusUnits: "pixels",
        lineWidthUnits: "pixels",
        stroked: true,
        pickable: true,
        updateTriggers: { getLineColor: s.selected, getLineWidth: s.selected, getRadius: s.selected },
        onClick: (info) => info.object && this.onPick((info.object as Alert).id),
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
    h("div", {}, "Degraded GNSS ratio", sw(GREEN, "<2%"), sw(AMBER, "2-10%"), sw(RED, ">10%")),
    h(
      "div",
      {},
      "Alerts",
      ...["gnss_jamming", "gnss_spoofing", "mil_orbit", "composite"].map((k) => sw(KIND_COLOR[k]!, k.replace("_", " "))),
    ),
  );
}
