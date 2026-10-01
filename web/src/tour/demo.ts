// Example visuals for the guided tour: drawn on the map only while a tour step asks for them,
// at a fixed spot in the Bay of Bengal, each labelled EXAMPLE. Not data: never stored or alerted.
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { LineLayer, PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { gridDisk, latLngToCell } from "h3-js";
import { circleRing } from "../aogeo";
import { GNSS_RGB } from "../spectrummodel";
import { monitorRgba, rgba, satRgba, surfaceRgba } from "../theme/palette";

export type DemoScene = "gnss" | "alert" | "heat" | "satellites" | "area";
export const DEMO_AT = { lon: 87.5, lat: 12.5 }; // Bay of Bengal: open sea, away from real areas
/** Camera per scene: lon, lat, zoom (satellites zoom out to the globe). */
export const DEMO_VIEW: Record<DemoScene, [number, number, number]> = {
  gnss: [DEMO_AT.lon, DEMO_AT.lat, 5.4],
  alert: [DEMO_AT.lon, DEMO_AT.lat, 5.6],
  heat: [DEMO_AT.lon, DEMO_AT.lat, 6.2],
  satellites: [DEMO_AT.lon, DEMO_AT.lat, 1.9],
  area: [DEMO_AT.lon, DEMO_AT.lat, 5.2],
};

export const tourDemo: { scene: DemoScene | null } = { scene: null };

const label = (id: string, lon: number, lat: number, text: string, z = 0): TextLayer<{ p: number[] }> =>
  new TextLayer<{ p: number[] }>({
    id,
    data: [{ p: [lon, lat, z] }],
    getPosition: (d) => d.p as [number, number, number],
    getText: () => text,
    getSize: 13,
    getColor: rgba("base", 1),
    getPixelOffset: [0, -22],
    fontWeight: 700,
    outlineWidth: 3,
    outlineColor: surfaceRgba(0.95),
    fontSettings: { sdf: true },
  });

/** Ring 0 red, ring 1 yellow, rings 2-3 green: what a jammed area looks like on the GNSS map. */
function gnssCells(): { h3: string; c: [number, number, number] }[] {
  const centre = latLngToCell(DEMO_AT.lat, DEMO_AT.lon, 4);
  const seen = new Set<string>();
  const out: { h3: string; c: [number, number, number] }[] = [];
  for (const [k, c] of [[0, GNSS_RGB.red], [1, GNSS_RGB.red], [2, GNSS_RGB.yellow], [4, GNSS_RGB.green]] as const) {
    for (const h of gridDisk(centre, k)) {
      if (seen.has(h)) continue;
      seen.add(h);
      out.push({ h3: h, c: [...c] as [number, number, number] });
    }
  }
  return out;
}

export function demoLayers(): unknown[] {
  const s = tourDemo.scene;
  if (!s) return [];
  const { lon, lat } = DEMO_AT;
  if (s === "gnss")
    return [
      new H3HexagonLayer<{ h3: string; c: [number, number, number] }>({
        id: "tour-gnss",
        data: gnssCells(),
        getHexagon: (d) => d.h3,
        getFillColor: (d) => [...d.c, 150],
        getLineColor: rgba("base", 0.3),
        lineWidthMinPixels: 1,
        stroked: true,
        extruded: false,
      }),
      label("tour-gnss-l", lon, lat + 2.2, "EXAMPLE · GPS interference map"),
    ];
  if (s === "alert")
    return [
      new ScatterplotLayer<{ p: number[] }>({
        id: "tour-alert",
        data: [{ p: [lon, lat] }],
        getPosition: (d) => d.p as [number, number],
        getRadius: 11,
        radiusUnits: "pixels",
        getFillColor: rgba("alert", 1),
        getLineColor: surfaceRgba(1),
        stroked: true,
        getLineWidth: 2,
        lineWidthUnits: "pixels",
      }),
      new ScatterplotLayer<{ p: number[] }>({
        id: "tour-warn",
        data: [{ p: [lon + 2.2, lat - 1.2] }],
        getPosition: (d) => d.p as [number, number],
        getRadius: 7,
        radiusUnits: "pixels",
        getFillColor: rgba("alert", 0.5),
      }),
      label("tour-alert-l", lon, lat, "EXAMPLE · critical alert"),
      label("tour-warn-l", lon + 2.2, lat - 1.2, "warning"),
    ];
  if (s === "heat") {
    const pts = [[0, 0], [0.15, 0.08], [-0.1, 0.12], [0.05, -0.14], [0.6, 0.4], [-0.5, -0.3]].map(([a, b]) => [lon + a!, lat + b!]);
    return [
      new ScatterplotLayer<number[]>({
        id: "tour-heat",
        data: pts,
        getPosition: (d) => d as [number, number],
        getRadius: 7,
        radiusUnits: "pixels",
        getFillColor: rgba("military", 0.55),
        getLineColor: rgba("military", 1),
        stroked: true,
        lineWidthUnits: "pixels",
        getLineWidth: 1.5,
      }),
      label("tour-heat-l", lon, lat + 0.3, "EXAMPLE · heat spots"),
    ];
  }
  if (s === "satellites") {
    const alt = 700_000;
    const track: [number, number, number][] = [];
    for (let i = -30; i <= 30; i++) track.push([lon - 6 + i * 0.35, lat + i * 1.6, alt]);
    return [
      new PathLayer<{ p: [number, number, number][] }>({ id: "tour-sat-track", data: [{ p: track }], getPath: (d) => d.p, getColor: satRgba(0.8), widthMinPixels: 2 }),
      new LineLayer<{ a: number[]; b: number[] }>({
        id: "tour-sat-drop",
        data: [{ a: [lon - 6, lat, 0], b: [lon - 6, lat, alt] }],
        getSourcePosition: (d) => d.a as [number, number, number],
        getTargetPosition: (d) => d.b as [number, number, number],
        getColor: satRgba(0.5),
      }),
      new ScatterplotLayer<{ p: number[] }>({
        id: "tour-sat",
        data: [{ p: [lon - 6, lat, alt] }],
        getPosition: (d) => d.p as [number, number, number],
        getRadius: 7,
        radiusUnits: "pixels",
        getFillColor: satRgba(1),
        getLineColor: rgba("base", 1),
        stroked: true,
        lineWidthUnits: "pixels",
        getLineWidth: 2,
      }),
      label("tour-sat-l", lon - 6, lat, "EXAMPLE · satellite, 700 km up", alt),
    ];
  }
  // area: a monitored area (green) and a drawn but unmonitored one (grey)
  return [
    new PathLayer<{ p: [number, number][]; on: boolean }>({
      id: "tour-ao",
      data: [
        { p: circleRing(lat, lon, 150), on: true },
        { p: circleRing(lat - 3.5, lon + 4.5, 90), on: false },
      ],
      getPath: (d) => d.p,
      getColor: (d) => (d.on ? monitorRgba(0.95) : rgba("neutral", 0.5)),
      widthMinPixels: 2.5,
    }),
    label("tour-ao-l", lon, lat + 2.6, "EXAMPLE · monitored area (green)"),
    label("tour-ao-l2", lon + 4.5, lat - 1.9, "not monitored (grey)"),
  ];
}
