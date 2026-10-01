// Navigation warnings and NOTAMs: hatched areas on the map plus a detail card.
// Notice text comes from third parties and is shown only via textContent inside an "unverified" box.
import { fmtDT } from "./tz";
import { GeoJsonLayer, PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import { api } from "./api";
import type { NoticeProps } from "./api";
import { h } from "./dom";
import { rgba } from "./theme/palette";
import { filters } from "./filters";
import { T } from "./labels";
import type { OpsMap } from "./map";
import { createWindow, EDGE_TOP, openWindow, setTitle } from "./wm";

type Ring = number[][];
interface Notice {
  props: NoticeProps;
  polys: Ring[][];
  point: [number, number] | null;
  hatch: number[][][];
}


function polygons(g: { type: string; coordinates: unknown } | null): Ring[][] {
  if (!g) return [];
  if (g.type === "Polygon") return [g.coordinates as Ring[]];
  if (g.type === "MultiPolygon") return g.coordinates as Ring[][];
  return [];
}

/** 45-degree hatch lines clipped to a polygon (even-odd across all its rings). */
function hatchLines(rings: Ring[]): number[][][] {
  const pts = rings.flat();
  if (pts.length < 3) return [];
  const xs = pts.map((p) => p[0]!);
  const ys = pts.map((p) => p[1]!);
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
  const step = Math.max(span / 14, 0.04);
  const f = (p: number[]): number => p[0]! - p[1]!;
  const fs = pts.map(f);
  const lo = Math.min(...fs);
  const hi = Math.max(...fs);
  const out: number[][][] = [];
  for (let c = lo + step / 2; c < hi; c += step) {
    const xsAt: number[][] = [];
    for (const ring of rings) {
      for (let i = 0; i < ring.length - 1; i++) {
        const a = ring[i]!;
        const b = ring[i + 1]!;
        const fa = f(a);
        const fb = f(b);
        if (fa < c !== fb < c) {
          const t = (c - fa) / (fb - fa);
          xsAt.push([a[0]! + t * (b[0]! - a[0]!), a[1]! + t * (b[1]! - a[1]!)]);
        }
      }
    }
    xsAt.sort((p, q) => p[0]! - q[0]!);
    for (let i = 0; i + 1 < xsAt.length; i += 2) out.push([xsAt[i]!, xsAt[i + 1]!]);
  }
  return out;
}

const fmtZ = (iso: string | null): string => (iso ? fmtDT(iso) : T.unknown);
const fmtBand = (lo: number | null, hi: number | null): string =>
  lo === null && hi === null ? T.unknown : `${lo === null ? "surface" : `${lo.toLocaleString()} ft`} to ${hi === null ? "unlimited" : `${hi.toLocaleString()} ft`}`;

export class Notices {
  private items: Notice[] = [];
  private card: HTMLElement;

  constructor(private ops: OpsMap) {
    this.card = createWindow({ id: "notice", title: T.notice, icon: "notice", x: -60, y: EDGE_TOP + 8, w: 360, h: 420, minW: 280 });
  }

  async refresh(): Promise<void> {
    try {
      const fc = await api.notices();
      this.items = fc.features.map((f) => {
        const polys = f.geometry ? polygons(f.geometry) : [];
        return {
          props: f.properties,
          polys,
          point: f.geometry?.type === "Point" ? (f.geometry.coordinates as [number, number]) : null,
          hatch: polys.flatMap(hatchLines),
        };
      });
    } catch {
      this.items = [];
    }
    this.ops.render();
  }

  layers(tool: string): unknown[] {
    if (!filters.notices || this.items.length === 0) return [];
    const pick = tool === "none";
    // official GNSS-interference notices (029d) stand out in the attention colour
    const isGnss = (n: Notice): boolean => n.props.category === "gnss_interference";
    const items = filters.gnssNoticesOnly ? this.items.filter(isGnss) : this.items;
    const shapes = items.filter((n) => n.polys.length > 0);
    return [
      new GeoJsonLayer<Notice>({
        id: "notice-areas",
        data: shapes.map((n) => ({ type: "Feature", properties: n, geometry: { type: "MultiPolygon", coordinates: n.polys } })) as never,
        filled: true,
        stroked: true,
        getFillColor: (f: { properties: Notice }) => (isGnss(f.properties) ? rgba("alert", 0.08) : rgba("notice", 0.1)),
        getLineColor: (f: { properties: Notice }) => (isGnss(f.properties) ? rgba("alert", 0.9) : rgba("notice", 0.9)),
        updateTriggers: { getFillColor: [filters.gnssNoticesOnly], getLineColor: [filters.gnssNoticesOnly] },
        lineWidthMinPixels: 1.5,
        pickable: pick,
        onClick: (i) => i.object && this.open((i.object as { properties: Notice }).properties),
      }),
      new PathLayer<number[][]>({
        id: "notice-hatch",
        data: shapes.flatMap((n) => n.hatch),
        getPath: (d) => d as [number, number][],
        getColor: rgba("notice", 0.47),
        widthMinPixels: 1,
      }),
      new ScatterplotLayer<Notice>({
        id: "notice-points",
        data: items.filter((n) => n.point),
        getPosition: (n) => n.point!,
        getRadius: 8,
        radiusUnits: "pixels",
        stroked: true,
        getFillColor: rgba("notice", 0.27),
        getLineColor: rgba("notice", 0.94),
        getLineWidth: 2,
        lineWidthUnits: "pixels",
        pickable: pick,
        onClick: (i) => i.object && this.open(i.object as Notice),
      }),
    ];
  }

  private open(n: Notice): void {
    const p = n.props;
    const kv = (rows: [string, string][]): HTMLElement => h("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
    setTitle("notice", p.kind === "notam" ? T.noticeNotam : T.noticeWarning);
    this.card.replaceChildren(
      h(
        "div",
        { class: "body" },
        h("h2", {}, p.title),
        kv([
          [T.reference, p.ref ?? T.unknown],
          [T.noticeType, [p.kind, p.category].filter(Boolean).join(" · ")],
          [T.validFrom, fmtZ(p.valid_from)],
          [T.validTo, fmtZ(p.valid_to)],
          [T.altitudeBand, fmtBand(p.alt_lower_ft, p.alt_upper_ft)],
          [T.source, p.source],
        ]),
        h("div", { class: "untrusted" }, h("div", { class: "note" }, T.originalText), h("pre", {}, p.text)),
      ),
    );
    openWindow("notice");
  }
}
