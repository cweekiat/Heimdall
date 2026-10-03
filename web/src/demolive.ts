// Demo on the live server (task 043). A demo pack (precomputed by scripts/midnight_hammer.py) holds, per step, a
// snapshot of the military aircraft recorded at that moment. While a demo is open the map shows that snapshot on
// its own layer and hides live traffic and live alert markers; closing the demo restores them. Nothing is replayed
// on the server: the pack is a static file, so live detection is unaffected.
import { PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import type { DemoAircraft } from "./api";
import { rgba } from "./theme/palette";

export const demoLive: { active: boolean; aircraft: DemoAircraft[]; at: string | null } = { active: false, aircraft: [], at: null };

const STRATEGIC = new Set(["tanker", "airlift", "isr", "c2", "bomber"]);

export function demoLiveLayers(): unknown[] {
  if (!demoLive.active || !demoLive.aircraft.length) return [];
  const rows = demoLive.aircraft;
  const key = demoLive.at ?? "";
  return [
    new PathLayer<DemoAircraft>({
      id: "demo-trails",
      data: rows.filter((r) => r.trail.length > 1),
      getPath: (r) => r.trail as [number, number][],
      getColor: (r) => (r.role === "isr" || r.role === "c2" ? [180, 142, 173, 200] : rgba("military", 0.75)),
      widthMinPixels: 2,
      updateTriggers: { getPath: key },
    }),
    new ScatterplotLayer<DemoAircraft>({
      id: "demo-aircraft",
      data: rows,
      getPosition: (r) => [r.lon, r.lat],
      getFillColor: (r) => (STRATEGIC.has(r.role) ? rgba("military", 1) : rgba("military", 0.45)),
      getLineColor: [255, 255, 255, 220],
      stroked: true,
      lineWidthMinPixels: 1,
      radiusMinPixels: 4,
      radiusMaxPixels: 7,
      getRadius: 4000,
      pickable: true,
      updateTriggers: { getPosition: key },
    }),
    new TextLayer<DemoAircraft>({
      id: "demo-labels",
      data: rows.filter((r) => STRATEGIC.has(r.role)),
      getPosition: (r) => [r.lon, r.lat],
      getText: (r) => r.flight || r.type || r.hex.toUpperCase(),
      getSize: 11,
      getPixelOffset: [0, -14],
      getColor: rgba("military", 1),
      fontWeight: 600,
      outlineWidth: 2,
      outlineColor: [0, 0, 0, 200],
      fontSettings: { sdf: true },
      updateTriggers: { getText: key, getPosition: key },
    }),
  ];
}
