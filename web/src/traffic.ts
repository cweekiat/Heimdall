// Live aircraft and ship layers (deck.gl IconLayer) plus their detail cards.
// Polls only the visible box: air every 5 s, ships every 30 s. Pauses while the tab is hidden and
// never overlaps requests. Colours are cool/neutral: red and amber stay reserved for alerts.
import { H3HexagonLayer } from "@deck.gl/geo-layers";
import { IconLayer, PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { api } from "./api";
import type { AirProps, SeaProps, SourceState } from "./api";
import { Feed } from "./trafficfeed";
import type { FeedReq } from "./trafficfeed";
import { COVERAGE_NOTE, densityCells, densityOpacity, iconOpacity, parseBox, truncationNote, wanted, zoomParam } from "./trafficdata";
import type { DensityRow, Wanted } from "./trafficdata";
import { rgba } from "./theme/palette";
import { ROLE_LABEL, roleRgba } from "./milroles";
import { altitudeMeters, dropLines, view } from "./map3d";
import { h } from "./dom";
import { icon, iconButton } from "./icons";
import { T } from "./labels";
import type { OpsMap } from "./map";
import { activeSources, filters, matchAir, matchSea, onFilters } from "./filters";
import { closeWindow, createWindow, EDGE_TOP, openWindow, setTitle } from "./wm";
import { state as store, selectAlert } from "./store";

type Pt = { lon: number; lat: number };
type Air = AirProps & Pt;
type Sea = SeaProps & Pt;
export type Selected = { kind: "air"; id: string } | { kind: "sea"; id: number };

const AIR_MS = 2000; // match the ~2 s writer flush; server-side query is bbox-limited
const SEA_MS = 30000;
const DECLUTTER_ZOOM = 5;
export { COVERAGE_NOTE };

// ---- sprite atlas, generated in code (white masks, tinted per feature by deck). Passed as a
// canvas, not a data: URL, because deck would fetch() the URL and CSP connect-src blocks data:.
const TILE = 64;
const NAMES = ["plane", "diamond", "ship"] as const;
function atlas(): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = TILE * NAMES.length;
  c.height = TILE;
  const g = c.getContext("2d")!;
  g.fillStyle = "white";
  g.strokeStyle = "white";
  const at = (i: number, f: () => void): void => {
    g.save();
    g.translate(i * TILE + TILE / 2, TILE / 2);
    f();
    g.restore();
  };
  at(0, () => {
    // aircraft silhouette pointing up
    g.beginPath();
    g.moveTo(0, -28);
    g.quadraticCurveTo(4, -22, 4, -10);
    g.lineTo(28, 6);
    g.lineTo(28, 12);
    g.lineTo(4, 4);
    g.lineTo(3, 18);
    g.lineTo(11, 24);
    g.lineTo(11, 28);
    g.lineTo(0, 25);
    g.lineTo(-11, 28);
    g.lineTo(-11, 24);
    g.lineTo(-3, 18);
    g.lineTo(-4, 4);
    g.lineTo(-28, 12);
    g.lineTo(-28, 6);
    g.lineTo(-4, -10);
    g.quadraticCurveTo(-4, -22, 0, -28);
    g.fill();
  });
  at(1, () => {
    // hollow diamond
    g.lineWidth = 7;
    g.beginPath();
    g.moveTo(0, -26);
    g.lineTo(22, 0);
    g.lineTo(0, 26);
    g.lineTo(-22, 0);
    g.closePath();
    g.stroke();
  });
  at(2, () => {
    // ship silhouette pointing up: pointed bow, flat stern, small bridge notch
    g.beginPath();
    g.moveTo(0, -28);
    g.quadraticCurveTo(13, -14, 13, -4);
    g.lineTo(13, 26);
    g.lineTo(-13, 26);
    g.lineTo(-13, -4);
    g.quadraticCurveTo(-13, -14, 0, -28);
    g.fill();
    g.globalCompositeOperation = "destination-out";
    g.fillRect(-6, 8, 12, 4); // bridge line so it reads as a hull, not an arrow
  });
  return c;
}
const MAPPING = Object.fromEntries(NAMES.map((n, i) => [n, { x: i * TILE, y: 0, width: TILE, height: TILE, mask: true, anchorY: TILE / 2 }]));

// ---- colours: every normal aircraft/ship/track is the neutral token (shape tells them apart);
// military is amber; anything in an alert is red (see theme/palette.ts).
const isMil = (r: { mil?: boolean; ship_class?: string }): boolean => r.mil === true || r.ship_class === "military";

/** Keep one marker per screen cell when zoomed far out (alert and military first). */
function declutter<T extends Pt & { in_alert: boolean; mil?: boolean }>(rows: T[], zoom: number): { row: T; n: number }[] {
  if (zoom >= DECLUTTER_ZOOM) return rows.map((row) => ({ row, n: 1 }));
  const cell = (360 / (512 * 2 ** zoom)) * 26;
  const best = new Map<string, { row: T; n: number }>();
  const rank = (r: T): number => (r.in_alert ? 2 : r.mil ? 1 : 0);
  for (const row of rows) {
    const k = `${Math.floor(row.lon / cell)}:${Math.floor(row.lat / cell)}`;
    const cur = best.get(k);
    if (!cur) best.set(k, { row, n: 1 });
    else {
      cur.n += 1;
      if (rank(row) > rank(cur.row)) cur.row = row;
    }
  }
  return [...best.values()];
}

const fmtAlt = (p: AirProps): string => (p.on_ground ? T.onGround : p.alt_baro === null ? T.unknown : `${Math.round(p.alt_baro).toLocaleString()} ft`);
const fmtSpeed = (v: number | null): string => (v === null ? T.unknown : `${Math.round(v)} kn`);
const fmtDeg = (v: number | null): string => (v === null ? T.unknown : `${Math.round(v)}°`);
const ageWord = (s: number): string => (s < 90 ? `${Math.round(s)} s ago` : `${Math.round(s / 60)} min ago`);

export class Traffic {
  private feeds: Record<"air" | "sea", { points: Feed<Air | Sea>; density: Feed<Air | Sea>; want: Wanted; src: string[] }>;
  private fv = 0; // bumps when filters change (cache key for the filtered/decluttered lists)
  private memo = new Map<string, { key: string; v: unknown }>();
  private banner: HTMLElement;
  private get air(): Air[] {
    return this.feeds.air.points.store.list() as Air[];
  }
  private get sea(): Sea[] {
    return this.feeds.sea.points.store.list() as Sea[];
  }
  private get airCells(): DensityRow[] {
    return this.feeds.air.density.cells;
  }
  private get seaCells(): DensityRow[] {
    return this.feeds.sea.density.cells;
  }
  private sources: SourceState[] = [];
  private counts: Record<string, number> = {};
  private trail: number[][] = [];
  private sel: Selected | null = null;
  private follow = false;
  private timers: number[] = [];
  private moveTimer = 0;
  private tip: HTMLElement;
  private card: HTMLElement;
  // deck accepts an image source at runtime; its typings only list string | Texture.
  private readonly icons = atlas() as unknown as string;
  onCounts: () => void = () => undefined;
  /** 047: open the investigation window for a military aircraft. */
  onInvestigate: ((hex: string) => void) | null = null;

  constructor(
    private ops: OpsMap,
    mapwrap: HTMLElement,
  ) {
    this.tip = h("div", { class: "hovercard tip", hidden: true });
    this.banner = h("div", { class: "p3-notice traffic-notice", role: "status", hidden: true });
    mapwrap.append(this.tip, this.banner);
    const mk = (d: "air" | "sea"): { points: Feed<Air | Sea>; density: Feed<Air | Sea>; want: Wanted; src: string[] } => {
      const fetcher = () => (r: FeedReq) =>
        api[d](r.bbox, this.feeds[d].src, r.zoom, { since: r.since, compact: r.compact, signal: r.signal });
      const key = (r: Air | Sea): string => String(d === "air" ? (r as Air).hex : (r as Sea).mmsi);
      const age = d === "air" ? 60_000 : 900_000;
      return { points: new Feed("points", key, age, fetcher()), density: new Feed("density", key, age, fetcher()), want: { points: false, density: true }, src: [] };
    };
    this.feeds = { air: mk("air"), sea: mk("sea") };
    this.card = createWindow({
      id: "entity",
      title: T.aircraft,
      icon: "plane",
      x: -60,
      y: EDGE_TOP + 8,
      w: 340,
      h: 460,
      minW: 280,
      onClose: () => {
        this.sel = null;
        this.follow = false;
        this.trail = [];
        this.ops.render();
      },
    });
    ops.onMove = () => {
      window.clearTimeout(this.moveTimer);
      this.moveTimer = window.setTimeout(() => void this.pollAll(), 350);
    };
    onFilters(() => {
      this.fv++;
      this.ops.render();
      if (this.sel) this.renderCard();
      void this.pollAll();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") void this.pollAll();
    });
  }

  setSources(s: SourceState[]): void {
    this.sources = s;
    this.onCounts();
  }
  getCounts = (): Record<string, number> => this.counts;
  /** Aircraft and ships currently loaded for the view (for the filter page). */
  totals = (): Record<string, number> => {
    // zoomed out the view is drawn from density cells, not individual points
    const n = (pts: number, cells: DensityRow[]): number => pts || cells.reduce((a, c) => a + c.count, 0);
    return { air: n(this.air.length, this.airCells), sea: n(this.sea.length, this.seaCells) };
  };

  /** Loaded aircraft/ships matching a callsign, hex, registration, ship name or MMSI (top search). */
  search(q: string, max = 6): { label: string; detail: string; lon: number; lat: number; pick: () => void }[] {
    const s = q.trim().toLowerCase();
    if (s.length < 2) return [];
    const out: { label: string; detail: string; lon: number; lat: number; pick: () => void }[] = [];
    for (const a of this.air) {
      if (out.length >= max) break;
      if (![a.flight, a.hex, a.registration].some((v) => v?.toLowerCase().includes(s))) continue;
      out.push({
        label: (a.flight ?? "").trim() || a.hex.toUpperCase(),
        detail: `Aircraft ${a.hex.toUpperCase()}${a.registration ? ` · ${a.registration}` : ""}`,
        lon: a.lon,
        lat: a.lat,
        pick: () => this.select({ kind: "air", id: a.hex }),
      });
    }
    for (const v of this.sea) {
      if (out.length >= max) break;
      if (!(v.name?.toLowerCase().includes(s) || String(v.mmsi).includes(s))) continue;
      out.push({ label: v.name || String(v.mmsi), detail: `Ship · MMSI ${v.mmsi}`, lon: v.lon, lat: v.lat, pick: () => this.select({ kind: "sea", id: v.mmsi }) });
    }
    return out;
  }

  start(): void {
    void this.pollAll();
    this.timers.push(window.setInterval(() => void this.pollAir(), AIR_MS), window.setInterval(() => void this.pollSea(), SEA_MS));
  }

  private pollAll(): Promise<unknown> {
    return Promise.all([this.pollDomain("air"), this.pollDomain("sea")]);
  }
  private pollAir = (): Promise<void> => this.pollDomain("air");
  private pollSea = (): Promise<void> => this.pollDomain("sea");

  /** Fetch the padded view for one domain. Feeds ignore duplicate requests, abort superseded ones and
   *  drop stale responses; here we only decide which datasets (points, density) the zoom needs. */
  private async pollDomain(d: "air" | "sea"): Promise<void> {
    if (document.visibilityState === "hidden") return;
    const f = this.feeds[d];
    const only = filters[d] ? activeSources(this.sources, d) : [];
    if (only && only.length === 0) {
      const a = f.points.clear();
      const b = f.density.clear();
      if (a || b) this.afterPoll();
      return;
    }
    f.src = only ?? [];
    const z = this.ops.zoom();
    f.want = wanted(f.want, z);
    const view = parseBox(this.ops.bbox());
    const sig = f.src.join(",");
    const jobs: Promise<boolean>[] = [];
    let changed = false;
    if (f.want.points) jobs.push(f.points.poll(view, zoomParam("points", z), sig));
    else changed = f.points.clear() || changed;
    if (f.want.density) jobs.push(f.density.poll(view, zoomParam("density", z), `${sig}|z${Math.floor(z)}`));
    else changed = f.density.clear() || changed;
    const res = await Promise.all(jobs);
    if (changed || res.some(Boolean)) this.afterPoll();
  }

  private recount(): void {
    const c: Record<string, number> = {};
    for (const a of this.air) for (const p of a.providers ?? [a.provider]) c[p] = (c[p] ?? 0) + 1;
    for (const s of this.sea) c[s.provider] = (c[s.provider] ?? 0) + 1;
    this.counts = c;
    this.onCounts();
  }

  private afterPoll(): void {
    this.recount();
    if (this.sel) {
      const e = this.find();
      if (e && this.follow) this.ops.panTo(e.lon, e.lat);
      this.renderCard();
    }
    this.drawBanner();
    this.ops.renderSoon();
  }

  private drawBanner(): void {
    const cut = (["air", "sea"] as const).flatMap((d) => {
      const p = this.feeds[d].points;
      return p.truncated ? [`${d === "air" ? T.aircraft : T.ships}: ${truncationNote(p.store.size, p.total)}`] : [];
    });
    this.banner.hidden = cut.length === 0;
    this.banner.textContent = cut.join(" ");
  }

  private find(): (Air | Sea) | undefined {
    const s = this.sel;
    if (!s) return undefined;
    return s.kind === "air" ? this.air.find((a) => a.hex === s.id) : this.sea.find((v) => v.mmsi === s.id);
  }

  private z(r: Air): number {
    return view.mode === "3d" ? altitudeMeters(r.alt_baro, r.on_ground, view.exaggeration) : 0;
  }

  /** Zoomed out: H3 density cells (opacity by log count); cells holding military get an amber outline.
   *  "Military only" (or any military role picked) keeps just the cells with military and counts only those (amber
   *  fill); density cells carry no role, so a role filter shows all military there until zoomed in. */
  private densityLayers(zoom: number): unknown[] {
    const fade = densityOpacity(zoom);
    const mil = filters.milOnly || filters.milRoles.length > 0;
    const mk = (id: string, all: DensityRow[], alpha: number): unknown[] => {
      const cells = densityCells(all, mil);
      if (!cells.length || fade <= 0.01) return [];
      const max = Math.log1p(Math.max(...cells.map((c) => c.count)));
      const a = (c: DensityRow): number => alpha * (0.15 + 0.85 * (Math.log1p(c.count) / max));
      return [
        new H3HexagonLayer<DensityRow>({
          id,
          data: cells,
          opacity: fade,
          getHexagon: (d) => d.h3,
          getFillColor: (d) => (mil ? rgba("military", a(d) * 0.6) : rgba("neutral", a(d))),
          updateTriggers: { getFillColor: [cells, mil], getLineColor: [mil] },
          getLineColor: (d) => (d.mil > 0 ? rgba("military", 0.9) : rgba("neutral", 0)),
          stroked: true,
          filled: true,
          extruded: false,
          lineWidthMinPixels: 1,
          pickable: false,
        }),
      ];
    };
    return [...mk("air-density", this.airCells, 0.85), ...mk("sea-density", this.seaCells, 0.5)];
  }

  // ---- deck layers
  private cached<V>(name: string, key: string, make: () => V): V {
    const hit = this.memo.get(name);
    if (hit && hit.key === key) return hit.v as V;
    const v = make();
    this.memo.set(name, { key, v });
    return v;
  }

  layers(tool: string, zoom: number): unknown[] {
    const fade = iconOpacity(zoom);
    const zq = zoom >= DECLUTTER_ZOOM ? 9 : Math.round(zoom * 4) / 4; // declutter cell size is quantised, so data is stable
    const da = this.cached("air", `${this.feeds.air.points.store.version}|${this.fv}|${zq}`, () => declutter(this.air.filter(matchAir), zq));
    const ds = this.cached("sea", `${this.feeds.sea.points.store.version}|${this.fv}|${zq}`, () => declutter(this.sea.filter(matchSea), zq));
    const pick = tool === "none" && fade > 0.5;
    const sel = this.sel;
    const out: unknown[] = [];
    if (this.trail.length > 1) {
      out.push(new PathLayer<{ p: number[][] }>({ id: "trail", data: [{ p: this.trail }], getPath: (d) => d.p as [number, number][], getColor: rgba("neutral", 0.9), widthMinPixels: 2.5 }));
    }
    out.push(...this.densityLayers(zoom));
    out.push(
      new IconLayer<{ row: Sea; n: number }>({
        id: "ships",
        data: ds,
        opacity: fade,
        visible: fade > 0.01,
        iconAtlas: this.icons,
        iconMapping: MAPPING,
        getIcon: () => "ship",
        getPosition: (d) => [d.row.lon, d.row.lat],
        getAngle: (d) => -(d.row.cog ?? d.row.heading ?? 0),
        getSize: (d) => ((d.row.sog ?? 0) > 0.5 ? 22 : 15),
        getColor: (d) => {
          const moving = (d.row.sog ?? 0) > 0.5; // moored or slow: same hull, smaller and dimmer
          return isMil(d.row) ? rgba("military", moving ? 0.95 : 0.6) : rgba("neutral", moving ? 0.9 : 0.5);
        },
        sizeUnits: "pixels",
        pickable: pick,
        onHover: (i) => this.hover(i.object ? `${(i.object as { row: Sea }).row.name ?? `MMSI ${(i.object as { row: Sea }).row.mmsi}`} · ${fmtSpeed((i.object as { row: Sea }).row.sog)}` : null, i.x, i.y),
        onClick: (i) => i.object && this.select({ kind: "sea", id: (i.object as { row: Sea }).row.mmsi }),
      }),
      new IconLayer<{ row: Air; n: number }>({
        id: "aircraft",
        data: da,
        opacity: fade,
        visible: fade > 0.01,
        iconAtlas: this.icons,
        iconMapping: MAPPING,
        getIcon: () => "plane", // military = the same plane in its role colour, amber family
        getPosition: (d) => [d.row.lon, d.row.lat, this.z(d.row)],
        getAngle: (d) => -(d.row.track ?? 0),
        updateTriggers: { getPosition: [view.mode, view.exaggeration] },
        getSize: (d) => (d.row.mil ? 28 : 26),
        getColor: (d) => (isMil(d.row) ? roleRgba(d.row.role, 1) : rgba("neutral", 0.95)),
        sizeUnits: "pixels",
        pickable: pick,
        onHover: (i) => {
          const r = (i.object as { row: Air } | undefined)?.row;
          this.hover(r ? [r.flight ?? r.hex, r.mil ? (r.type_name ?? r.type_code) : null, r.mil ? ROLE_LABEL[r.role ?? "other"] : null, fmtAlt(r)].filter(Boolean).join(" · ") : null, i.x, i.y);
        },
        onClick: (i) => i.object && this.select({ kind: "air", id: (i.object as { row: Air }).row.hex }),
      }),
      ...(view.mode === "3d" && view.drop
        ? [
            new PathLayer<[number, number, number][]>({
              id: "aircraft-drop",
              data: this.cached("drop", `${this.feeds.air.points.store.version}|${this.fv}|${zq}|${view.exaggeration}`, () => dropLines(da.map((d) => d.row), true, view.exaggeration)),
              getPath: (d) => d,
              getColor: rgba("neutral", 0.5),
              widthMinPixels: 1,
            }),
          ]
        : []),
      // thin amber ring: GPS accuracy problem on that aircraft
      new ScatterplotLayer<{ row: Air }>({
        id: "gps-ring",
        opacity: fade,
        data: this.cached("gps", `${this.feeds.air.points.store.version}|${this.fv}|${zq}`, () => da.filter((d) => d.row.gps_bad)),
        getPosition: (d) => [d.row.lon, d.row.lat],
        getRadius: 13,
        radiusUnits: "pixels",
        stroked: true,
        filled: false,
        getLineColor: rgba("alert", 0.9),
        getLineWidth: 1.2,
        lineWidthUnits: "pixels",
      }),
      // halo: part of an alert
      new ScatterplotLayer<{ row: Air | Sea }>({
        id: "alert-halo",
        opacity: fade,
        data: this.cached("halo", `${this.feeds.air.points.store.version}|${this.feeds.sea.points.store.version}|${this.fv}|${zq}`, () => [...da, ...ds].filter((d) => d.row.in_alert)),
        getPosition: (d) => [d.row.lon, d.row.lat],
        getRadius: 18,
        radiusUnits: "pixels",
        stroked: true,
        filled: true,
        getFillColor: rgba("alert", 0.16),
        getLineColor: rgba("alert", 0.86),
        getLineWidth: 2,
        lineWidthUnits: "pixels",
      }),
    );
    if (sel) {
      const e = this.find();
      if (e) {
        out.push(
          new ScatterplotLayer<Pt>({
            id: "entity-sel",
            data: [e],
            getPosition: (d) => [d.lon, d.lat],
            getRadius: 20,
            radiusUnits: "pixels",
            stroked: true,
            filled: false,
            getLineColor: rgba("base", 1),
            getLineWidth: 2,
            lineWidthUnits: "pixels",
          }),
        );
      }
    }
    if (zoom < DECLUTTER_ZOOM) {
      out.push(
        new TextLayer<{ row: Air | Sea; n: number }>({
          id: "traffic-n",
          data: [...da, ...ds].filter((d) => d.n > 1),
          getPosition: (d) => [d.row.lon, d.row.lat],
          getText: (d) => String(d.n),
          getSize: 11,
          getColor: rgba("base", 1),
          getPixelOffset: [12, -12],
          fontWeight: 700,
        }),
      );
    }
    return out;
  }

  private hover(text: string | null, x: number, y: number): void {
    if (!text) {
      this.tip.hidden = true;
      return;
    }
    this.tip.hidden = false;
    this.tip.style.left = `${x + 12}px`;
    this.tip.style.top = `${y + 12}px`;
    this.tip.textContent = text;
  }

  // ---- detail cards
  select(s: Selected): void {
    this.sel = s;
    this.follow = false;
    this.trail = [];
    this.hover(null, 0, 0);
    this.renderCard();
    openWindow("entity");
    void this.loadTrail(s);
    this.ops.render();
  }

  private async loadTrail(s: Selected): Promise<void> {
    try {
      const f = s.kind === "air" ? await api.airTrail(s.id) : await api.seaTrail(s.id);
      if (this.sel && this.sel.kind === s.kind && this.sel.id === s.id) this.trail = f.geometry.coordinates;
    } catch {
      this.trail = []; // 404 = no recent positions
    }
    this.ops.render();
  }

  /** Alerts whose evidence names this aircraft or ship. Checks open alerts only. */
  private async relatedAlert(s: Selected): Promise<string | null> {
    const key = s.kind === "air" ? s.id.toLowerCase() : String(s.id);
    const open = store.alerts.filter((a) => ["new", "acknowledged", "under_review"].includes(a.status)).slice(0, 30);
    for (const a of open) {
      try {
        const d = await api.alert(a.id);
        if (d.evidence.some((e) => e.entities.some((x) => x.toLowerCase() === key))) return a.id;
      } catch {
        /* skip */
      }
    }
    return null;
  }

  private renderCard(): void {
    const s = this.sel;
    if (!s) return;
    const e = this.find();
    const root = this.card;
    const air = s.kind === "air" ? (e as Air | undefined) : undefined;
    const sea = s.kind === "sea" ? (e as Sea | undefined) : undefined;
    setTitle("entity", s.kind === "air" ? T.aircraft : T.ship);
    if (!e) {
      root.replaceChildren(h("div", { class: "body" }, h("div", { class: "note" }, T.notInView)));
      return;
    }
    const kv = (rows: [string, string | Node][]): HTMLElement =>
      h("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
    const gps = air ? h("span", { class: `badge ${air.gps_bad ? "warn" : "ok"}` }, air.gps_bad ? T.gpsProblem : T.gpsOk) : null;
    const rel = h("button", { type: "button", disabled: !e.in_alert, title: e.in_alert ? "" : T.notInAlert }, icon("alerts", 14), T.showRelated);
    rel.addEventListener("click", () => {
      void this.relatedAlert(s).then((id) => {
        rel.textContent = id ? T.showRelated : T.noRelated;
        if (id) void selectAlert(id);
      });
    });
    const follow = h("label", { class: "chk" });
    const fi = h("input", { type: "checkbox" });
    fi.checked = this.follow;
    fi.addEventListener("change", () => {
      this.follow = fi.checked;
      if (this.follow) this.ops.panTo(e.lon, e.lat);
    });
    follow.append(fi, icon("follow", 14), T.follow);
    root.replaceChildren(
      h(
        "div",
        { class: "body ent" },
        h("h2", {}, air ? (air.flight ?? air.hex) : (sea!.name ?? `MMSI ${sea!.mmsi}`)),
        air
          ? kv([
              [T.callsign, air.flight ?? T.unknown],
              [T.registration, air.registration ?? T.unknown],
              [T.aircraftType, air.type_code ?? T.unknown],
              [T.altitude, fmtAlt(air)],
              [T.speed, fmtSpeed(air.gs)],
              [T.heading, fmtDeg(air.track)],
              [T.squawk, air.squawk ?? T.unknown],
              [T.gps, gps!],
              [T.reportedBy, (air.providers ?? [air.provider]).join(", ")],
              [T.lastHeard, ageWord(air.age_s)],
              ...(air.mil
                ? ([
                    [T.military, ROLE_LABEL[air.role ?? "other"] ?? T.yes],
                    ["Type", air.type_name ?? air.type_code ?? T.unknown],
                    ["Operator", air.operator ?? T.unknown],
                    ["Country", air.country ?? T.unknown],
                  ] as [string, string][])
                : []),
            ])
          : kv([
              [T.shipName, sea!.name ?? T.unknown],
              ["MMSI", String(sea!.mmsi)],
              [T.shipType, T.shipClass[sea!.ship_class] ?? sea!.ship_class],
              [T.speed, fmtSpeed(sea!.sog)],
              [T.course, fmtDeg(sea!.cog)],
              [T.destination, sea!.destination ?? T.unknown],
              [T.status, (sea!.sog ?? 0) > 0.5 ? T.underway : T.stationary],
              [T.reportedBy, sea!.provider],
              [T.lastHeard, ageWord(sea!.age_s)],
            ]),
        h("div", { class: "note" }, s.kind === "air" ? T.trailAir : T.trailSea, this.trail.length > 1 ? "" : ` (${T.noTrail})`),
        h(
          "div",
          { class: "row" },
          follow,
          rel,
          air?.mil && this.onInvestigate ? h("button", { type: "button", class: "primary", onclick: () => this.onInvestigate?.(air.hex) }, icon("plane", 14), "Investigate") : null,
          iconButton("close", T.closeCard, () => closeWindow("entity")),
        ),
      ),
    );
  }

  /** Clear the picked entity from outside (Esc handling is done by the window manager). */
  dispose(): void {
    this.timers.forEach((t) => window.clearInterval(t));
  }
}
