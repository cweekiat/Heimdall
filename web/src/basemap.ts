// Base styles + toggleable overlays for the operational map.
// Heimdall's own layers (hexes, alerts, tracks, ellipses, AOIs) are drawn by a separate deck.gl
// MapboxOverlay canvas (interleaved: false), so they stay on top of any base or overlay and are
// unaffected by map.setStyle().
import type * as maplibregl from "maplibre-gl";
import { h } from "./dom.ts";
import { css, surfaceCss } from "./theme/palette.ts";
import { absolutizeStyle, offlineStyleUrl } from "./map3d.ts";

export type Provider = "openfreemap" | "carto" | "none";
type Style = maplibregl.StyleSpecification | string;
type Theme = "dark" | "light";

const OFM = "https://tiles.openfreemap.org";
const GLYPHS = `${OFM}/fonts/{fontstack}/{range}.pbf`;
const OFM_ATTR =
  '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> ' +
  '<a href="https://www.openmaptiles.org/" target="_blank">&copy; OpenMapTiles</a> ' +
  'Data from <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>';

interface BaseDef {
  id: string;
  label: string;
  thirdParty: boolean;
  style: () => Style;
}
interface OverlayDef {
  id: string;
  label: string;
  license: string;
  thirdParty: boolean;
}

function graticule(): unknown {
  const features: unknown[] = [];
  const line = (coordinates: number[][]) => ({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates } });
  for (let lon = -180; lon <= 180; lon += 5) features.push(line([[lon, -85], [lon, 85]]));
  for (let lat = -80; lat <= 80; lat += 5) features.push(line([[-180, lat], [180, lat]]));
  return { type: "FeatureCollection", features };
}

const blank = (theme: Theme): Style => ({
  version: 8,
  sources: {},
  layers: [{ id: "bg", type: "background", paint: { "background-color": surfaceCss(theme) } }],
});

const satellite = (): Style => ({
  version: 8,
  glyphs: GLYPHS,
  sources: {
    sat: {
      type: "raster",
      tiles: ["https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg"],
      tileSize: 256,
      maxzoom: 13,
      attribution:
        '<a href="https://s2maps.eu" target="_blank">Sentinel-2 cloudless</a> by <a href="https://eox.at" target="_blank">EOX</a> &mdash; CC BY-NC-SA 4.0 (contains modified Copernicus Sentinel data)',
    },
  },
  layers: [{ id: "sat", type: "raster", source: "sat" }],
});

export const OVERLAYS: OverlayDef[] = [
  { id: "borders", label: "Country borders + labels", license: "OSM (ODbL) via OpenFreeMap", thirdParty: true },
  { id: "military", label: "Military areas + airports", license: "OSM (ODbL) via OpenFreeMap", thirdParty: true },
  { id: "eez", label: "EEZ boundaries", license: "Marine Regions (VLIZ), CC-BY", thirdParty: true },
  { id: "seamarks", label: "Seamarks (OpenSeaMap)", license: "OpenSeaMap contributors, ODbL", thirdParty: true },
  { id: "graticule", label: "Graticule (5 deg)", license: "local", thirdParty: false },
];

function basesFor(provider: Provider, online: boolean): BaseDef[] {
  if (provider === "none" || !online)
    return [
      { id: "blank-dark", label: "Blank dark (private)", thirdParty: false, style: () => blank("dark") },
      { id: "blank-light", label: "Blank light (private)", thirdParty: false, style: () => blank("light") },
    ];
  const vec: BaseDef[] =
    provider === "carto"
      ? [
          { id: "dark", label: "Dark", thirdParty: true, style: () => "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json" },
          { id: "light", label: "Light", thirdParty: true, style: () => "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json" },
        ]
      : [
          { id: "dark", label: "Dark", thirdParty: true, style: () => `${OFM}/styles/dark` },
          { id: "light", label: "Light", thirdParty: true, style: () => `${OFM}/styles/positron` },
          { id: "liberty", label: "Detailed", thirdParty: true, style: () => `${OFM}/styles/liberty` },
          { id: "bright", label: "Bright", thirdParty: true, style: () => `${OFM}/styles/bright` },
        ];
  return [...vec, { id: "satellite", label: "Satellite (Sentinel-2)", thirdParty: true, style: satellite }];
}

function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function safeSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode): choices just are not persisted */
  }
}

const HATCH = 12;
function hatch(): ImageData {
  const c = document.createElement("canvas");
  c.width = c.height = HATCH;
  const g = c.getContext("2d")!;
  g.strokeStyle = css("military", 0.85);
  g.lineWidth = 2;
  g.beginPath();
  g.moveTo(0, HATCH);
  g.lineTo(HATCH, 0);
  g.moveTo(-2, 2);
  g.lineTo(2, -2);
  g.moveTo(HATCH - 2, HATCH + 2);
  g.lineTo(HATCH + 2, HATCH - 2);
  g.stroke();
  return g.getImageData(0, 0, HATCH, HATCH);
}

export class BaseLayers {
  private bases: BaseDef[];
  private baseId: string;
  private overlays: Set<string>;
  private online: boolean;
  private theme: Theme;
  private panelEl = h("div", { class: "layerbody" });

  private map: maplibregl.Map;
  private provider: Provider;

  constructor(map: maplibregl.Map, provider: Provider, defaultOverlays: string[], theme: Theme) {
    this.map = map;
    this.provider = provider;
    this.theme = theme;
    this.online = provider !== "none" && safeGet("hm.online") === "1";
    this.bases = this.allBases();
    const stored = safeGet("hm.base");
    this.baseId = this.bases.some((b) => b.id === stored) ? (stored as string) : this.themeBase();
    let ov = defaultOverlays;
    try {
      const raw = safeGet("hm.overlays");
      if (raw) ov = (JSON.parse(raw) as string[]).filter((x) => OVERLAYS.some((o) => o.id === x));
    } catch {
      /* ignore corrupt value */
    }
    this.overlays = new Set(!this.online ? ov.filter((x) => !OVERLAYS.find((o) => o.id === x)?.thirdParty) : ov);
    this.map.on("style.load", () => {
      try {
        this.onStyleLoad();
        this.refresh();
      } catch (e) {
        console.warn("overlay refresh failed", e);
      }
    });
  }

  private allBases(): BaseDef[] {
    const offline: BaseDef = { id: "offline", label: "Offline map (local, default)", thirdParty: false, style: () => offlineStyleUrl(this.theme) };
    return [offline, ...basesFor(this.provider, this.online)];
  }

  private themeBase(): string {
    return "offline";
  }

  /** Fires when the base changes so the app can check the offline files are present. */
  onBaseChange: (id: string) => void = () => undefined;
  /** Fires after any base/overlay/online change (map-type picker, settings page). */
  onListChange: () => void = () => undefined;

  /** Base maps on offer, for the map-type picker. */
  list(): { id: string; label: string; thirdParty: boolean }[] {
    return this.bases.map((b) => ({ id: b.id, label: b.label, thirdParty: b.thirdParty }));
  }

  currentId(): string {
    return this.baseId;
  }

  isOnline(): boolean {
    return this.online;
  }

  overlayList(): { id: string; label: string; license: string; on: boolean }[] {
    return OVERLAYS.filter((o) => this.online || !o.thirdParty).map((o) => ({ id: o.id, label: o.label, license: o.license, on: this.overlays.has(o.id) }));
  }

  /** Projection etc. re-applied by the map after every style load. */
  onStyleLoad: () => void = () => undefined;

  setOnline(on: boolean): void {
    this.online = on && this.provider !== "none";
    safeSet("hm.online", this.online ? "1" : "0");
    this.bases = this.allBases();
    if (!this.bases.some((b) => b.id === this.baseId)) this.baseId = "offline";
    if (!this.online) for (const o of OVERLAYS) if (o.thirdParty) this.overlays.delete(o.id);
    this.setBase(this.baseId);
  }

  /** The offline style has root-relative sprite/glyph URLs; workers need absolute ones. */
  private styleOpts(): never {
    return { diff: false, transformStyle: (_p: unknown, next: never) => absolutizeStyle(next, location.origin) } as never;
  }

  /** Initial style to construct the map with. */
  initialStyle(): Style {
    return this.current().style();
  }

  /** Apply the initial style (with the origin fix). */
  applyInitial(): void {
    this.map.setStyle(this.initialStyle(), this.styleOpts());
    this.onBaseChange(this.baseId);
  }

  private current(): BaseDef {
    return this.bases.find((b) => b.id === this.baseId) ?? this.bases[0]!;
  }

  setTheme(theme: Theme): void {
    this.theme = theme;
    // Only follow the theme when a theme-paired base is showing; keep satellite/detailed as chosen.
    if (this.baseId === "offline") this.setBase("offline");
    else if (["dark", "light"].includes(this.baseId)) this.setBase(theme);
    else if (["blank-dark", "blank-light"].includes(this.baseId)) this.setBase(`blank-${theme}`);
  }

  setBase(id: string): void {
    this.baseId = id;
    safeSet("hm.base", id);
    this.map.setStyle(this.current().style(), this.styleOpts());
    this.renderPanel();
    this.onBaseChange(id);
    this.onListChange();
  }

  toggleOverlay(id: string, on: boolean): void {
    if (on) this.overlays.add(id);
    else this.overlays.delete(id);
    safeSet("hm.overlays", JSON.stringify([...this.overlays]));
    this.refresh();
    this.renderPanel();
    this.onListChange();
  }

  /** Remove every hm-* layer/source and re-add the enabled overlays in canonical order. */
  private refresh(): void {
    const m = this.map;
    if (!m.isStyleLoaded() && !m.getStyle()) return;
    for (const l of [...(m.getStyle().layers ?? [])]) if (l.id.startsWith("hm-")) m.removeLayer(l.id);
    for (const id of Object.keys(m.getStyle().sources ?? {})) if (id.startsWith("hm-")) m.removeSource(id);

    const on = (id: string) => this.overlays.has(id);
    const dark = this.baseId === "dark" || this.baseId === "satellite" || this.baseId === "blank-dark" || (this.baseId === "offline" && this.theme === "dark");
    const mode: Theme = dark ? "dark" : "light";
    const needsVector = on("borders") || on("military");
    if (needsVector) {
      m.addSource("hm-ofm", { type: "vector", url: `${OFM}/planet`, attribution: OFM_ATTR });
    }
    if (on("eez")) {
      m.addSource("hm-eez", {
        type: "raster",
        tileSize: 256,
        tiles: [
          "https://geo.vliz.be/geoserver/MarineRegions/wms?service=WMS&version=1.1.1&request=GetMap&layers=MarineRegions:eez_boundaries&styles=&format=image/png&transparent=true&srs=EPSG:3857&width=256&height=256&bbox={bbox-epsg-3857}",
        ],
        attribution: '<a href="https://www.marineregions.org" target="_blank">Marine Regions</a> EEZ (CC-BY)',
      });
      m.addLayer({ id: "hm-eez", type: "raster", source: "hm-eez", paint: { "raster-opacity": 0.9 } });
    }
    if (on("seamarks")) {
      m.addSource("hm-seamarks", {
        type: "raster",
        tileSize: 256,
        maxzoom: 18,
        tiles: ["https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"],
        attribution: '<a href="https://www.openseamap.org" target="_blank">OpenSeaMap</a> contributors (ODbL)',
      });
      m.addLayer({ id: "hm-seamarks", type: "raster", source: "hm-seamarks" });
    }
    if (on("graticule")) {
      m.addSource("hm-grid", { type: "geojson", data: graticule() as never });
      m.addLayer({
        id: "hm-grid",
        type: "line",
        source: "hm-grid",
        paint: { "line-color": css("neutral", 1, mode), "line-width": 0.6, "line-opacity": 0.6 },
      });
    }
    if (on("military")) {
      if (!m.hasImage("hm-hatch")) m.addImage("hm-hatch", hatch());
      m.addLayer({
        id: "hm-mil-fill",
        type: "fill",
        source: "hm-ofm",
        "source-layer": "landuse",
        filter: ["==", ["get", "class"], "military"] as never,
        paint: { "fill-pattern": "hm-hatch", "fill-opacity": 0.9 },
      });
      m.addLayer({
        id: "hm-mil-line",
        type: "line",
        source: "hm-ofm",
        "source-layer": "landuse",
        filter: ["==", ["get", "class"], "military"] as never,
        paint: { "line-color": css("military", 1, mode), "line-width": 1.2 },
      });
      m.addLayer({
        id: "hm-runways",
        type: "line",
        source: "hm-ofm",
        "source-layer": "aeroway",
        minzoom: 8,
        filter: ["in", ["get", "class"], ["literal", ["runway", "taxiway"]]] as never,
        paint: { "line-color": css("neutral", 1, mode), "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1, 14, 5] as never },
      });
      m.addLayer({
        id: "hm-airports",
        type: "circle",
        source: "hm-ofm",
        "source-layer": "aerodrome_label",
        minzoom: 4,
        paint: {
          "circle-radius": 5,
          "circle-color": ["case", ["==", ["get", "class"], "military"], css("military", 1, mode), css("neutral", 1, mode)] as never,
          "circle-stroke-color": surfaceCss(mode),
          "circle-stroke-width": 1,
        },
      });
    }
    if (on("borders")) {
      const country: unknown = ["all", ["==", ["get", "admin_level"], 2], ["!=", ["get", "maritime"], 1], ["!=", ["get", "disputed"], 1]];
      m.addLayer({
        id: "hm-border-country",
        type: "line",
        source: "hm-ofm",
        "source-layer": "boundary",
        filter: country as never,
        paint: {
          "line-color": css("base", 1, mode),
          "line-width": ["interpolate", ["linear"], ["zoom"], 1, 0.8, 6, 1.8, 10, 3] as never,
          "line-opacity": 0.9,
        },
      });
      m.addLayer({
        id: "hm-border-disputed",
        type: "line",
        source: "hm-ofm",
        "source-layer": "boundary",
        filter: ["all", ["==", ["get", "admin_level"], 2], ["==", ["get", "disputed"], 1]] as never,
        paint: { "line-color": css("notice", 1, mode), "line-width": 1.6, "line-dasharray": [3, 2] },
      });
    }
    // Text layers need glyphs; CARTO styles use a different font stack, so labels are skipped there.
    const labelsOk = this.provider !== "carto";
    if (labelsOk && on("borders")) {
      m.addLayer({
        id: "hm-country-labels",
        type: "symbol",
        source: "hm-ofm",
        "source-layer": "place",
        filter: ["==", ["get", "class"], "country"] as never,
        layout: {
          "text-field": ["coalesce", ["get", "name:en"], ["get", "name"]] as never,
          "text-font": ["Noto Sans Bold"],
          "text-size": ["interpolate", ["linear"], ["zoom"], 2, 11, 6, 16] as never,
          "text-transform": "uppercase",
          "text-letter-spacing": 0.08,
        },
        paint: {
          "text-color": css("base", 1, mode),
          "text-halo-color": surfaceCss(mode),
          "text-halo-width": 1.6,
        },
      });
    }
    if (labelsOk && on("military")) {
      m.addLayer({
        id: "hm-airport-labels",
        type: "symbol",
        source: "hm-ofm",
        "source-layer": "aerodrome_label",
        minzoom: 5,
        layout: {
          "text-field": ["coalesce", ["get", "iata"], ["get", "name:en"], ["get", "name"]] as never,
          "text-font": ["Noto Sans Bold"],
          "text-size": 11,
          "text-offset": [0, 1.1],
          "text-anchor": "top",
        },
        paint: {
          "text-color": css("military", 1, mode),
          "text-halo-color": surfaceCss(mode),
          "text-halo-width": 1.5,
        },
      });
    }
  }

  /** The layer-switcher panel (DOM). */
  panel(): HTMLElement {
    this.renderPanel();
    return this.panelEl;
  }

  private renderPanel(): void {
    const third = this.online;
    this.panelEl.replaceChildren(
      h("fieldset", {}, h("legend", {}, "Map overlays"),
        ...OVERLAYS.filter((o) => third || !o.thirdParty).map((o) =>
          h("label", { title: o.license }, h("input", { type: "checkbox", checked: this.overlays.has(o.id), onchange: (e) => this.toggleOverlay(o.id, (e.target as HTMLInputElement).checked) }), o.label),
        ),
      ),
      this.provider === "none"
        ? h("p", { class: "note" }, "Online map styles are disabled (ui.basemap: none).")
        : h("label", {}, h("input", { type: "checkbox", checked: this.online, onchange: (e) => this.setOnline((e.target as HTMLInputElement).checked) }), "Allow online map styles (third-party tiles)"),
      h("p", { class: "note" }, third ? "Map tiles from third parties see viewed areas. Set ui.basemap: none for a fully private map." : "Private mode: no third-party requests."),
    );
  }
}
