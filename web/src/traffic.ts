// Live aircraft and ship layers (deck.gl IconLayer) plus their detail cards.
// Polls only the visible box: air every 5 s, ships every 30 s. Pauses while the tab is hidden and
// never overlaps requests. Colours are cool/neutral: red and amber stay reserved for alerts.
import { IconLayer, PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { api } from "./api";
import type { AirProps, FC, SeaProps, SourceState } from "./api";
import { rgba } from "./theme/palette";
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

const AIR_MS = 5000;
const SEA_MS = 30000;
const DECLUTTER_ZOOM = 5;

// ---- sprite atlas, generated in code (white masks, tinted per feature by deck). Passed as a
// canvas, not a data: URL, because deck would fetch() the URL and CSP connect-src blocks data:.
const TILE = 64;
const NAMES = ["plane", "diamond", "arrow", "dot"] as const;
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
    // ship arrow pointing up
    g.beginPath();
    g.moveTo(0, -28);
    g.lineTo(18, 24);
    g.lineTo(0, 14);
    g.lineTo(-18, 24);
    g.closePath();
    g.fill();
  });
  at(3, () => {
    g.beginPath();
    g.arc(0, 0, 14, 0, Math.PI * 2);
    g.fill();
  });
  return c;
}
const MAPPING = Object.fromEntries(NAMES.map((n, i) => [n, { x: i * TILE, y: 0, width: TILE, height: TILE, mask: true, anchorY: TILE / 2 }]));

// ---- colours: every normal aircraft/ship/track is the neutral token (shape tells them apart);
// military is amber; anything in an alert is red (see theme/palette.ts).
const isMil = (r: { mil?: boolean; ship_class?: string }): boolean => r.mil === true || r.ship_class === "military";

const flat = <P extends object>(fc: FC<P>): (P & Pt)[] =>
  fc.features
    .filter((f) => f.geometry.type === "Point")
    .map((f) => {
      const [lon, lat] = f.geometry.coordinates as [number, number];
      return { ...f.properties, lon, lat };
    });

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
  private air: Air[] = [];
  private sea: Sea[] = [];
  private sources: SourceState[] = [];
  private counts: Record<string, number> = {};
  private trail: number[][] = [];
  private sel: Selected | null = null;
  private follow = false;
  private busy = { air: false, sea: false };
  private again = { air: false, sea: false };
  private timers: number[] = [];
  private moveTimer = 0;
  private tip: HTMLElement;
  private card: HTMLElement;
  // deck accepts an image source at runtime; its typings only list string | Texture.
  private readonly icons = atlas() as unknown as string;
  onCounts: () => void = () => undefined;

  constructor(
    private ops: OpsMap,
    mapwrap: HTMLElement,
  ) {
    this.tip = h("div", { class: "hovercard tip", hidden: true });
    mapwrap.append(this.tip);
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

  start(): void {
    void this.pollAll();
    this.timers.push(window.setInterval(() => void this.pollAir(), AIR_MS), window.setInterval(() => void this.pollSea(), SEA_MS));
  }

  private pollAll(): Promise<unknown> {
    return Promise.all([this.pollAir(), this.pollSea()]);
  }

  private async pollAir(): Promise<void> {
    if (document.visibilityState === "hidden") return;
    if (this.busy.air) {
      this.again.air = true;
      return;
    }
    if (!filters.air) {
      if (this.air.length) {
        this.air = [];
        this.recount();
        this.ops.render();
      }
      return;
    }
    this.busy.air = true;
    try {
      const only = activeSources(this.sources, "air");
      this.air = only && only.length === 0 ? [] : flat(await api.air(this.ops.bbox(), only ?? []));
    } catch {
      /* keep the last picture; the status strip shows source health */
    } finally {
      this.busy.air = false;
    }
    this.afterPoll();
    if (this.again.air) {
      this.again.air = false;
      void this.pollAir();
    }
  }

  private async pollSea(): Promise<void> {
    if (document.visibilityState === "hidden") return;
    if (this.busy.sea) {
      this.again.sea = true;
      return;
    }
    if (!filters.sea) {
      if (this.sea.length) {
        this.sea = [];
        this.recount();
        this.ops.render();
      }
      return;
    }
    this.busy.sea = true;
    try {
      const only = activeSources(this.sources, "sea");
      this.sea = only && only.length === 0 ? [] : flat(await api.sea(this.ops.bbox(), only ?? []));
    } catch {
      /* keep the last picture */
    } finally {
      this.busy.sea = false;
    }
    this.afterPoll();
    if (this.again.sea) {
      this.again.sea = false;
      void this.pollSea();
    }
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
    this.ops.renderSoon();
  }

  private find(): (Air | Sea) | undefined {
    const s = this.sel;
    if (!s) return undefined;
    return s.kind === "air" ? this.air.find((a) => a.hex === s.id) : this.sea.find((v) => v.mmsi === s.id);
  }

  private z(r: Air): number {
    return view.mode === "3d" ? altitudeMeters(r.alt_baro, r.on_ground, view.exaggeration) : 0;
  }

  // ---- deck layers
  layers(tool: string, zoom: number): unknown[] {
    const pick = tool === "none";
    const air = this.air.filter(matchAir);
    const sea = this.sea.filter(matchSea);
    const da = declutter(air, zoom);
    const ds = declutter(sea, zoom);
    const sel = this.sel;
    const out: unknown[] = [];
    if (this.trail.length > 1) {
      out.push(new PathLayer<{ p: number[][] }>({ id: "trail", data: [{ p: this.trail }], getPath: (d) => d.p as [number, number][], getColor: rgba("neutral", 0.9), widthMinPixels: 2.5 }));
    }
    out.push(
      new IconLayer<{ row: Sea; n: number }>({
        id: "ships",
        data: ds,
        iconAtlas: this.icons,
        iconMapping: MAPPING,
        getIcon: (d) => ((d.row.sog ?? 0) > 0.5 ? "arrow" : "dot"),
        getPosition: (d) => [d.row.lon, d.row.lat],
        getAngle: (d) => -(d.row.cog ?? d.row.heading ?? 0),
        getSize: (d) => ((d.row.sog ?? 0) > 0.5 ? 15 : 10),
        getColor: (d) => (isMil(d.row) ? rgba("military", 0.95) : rgba("neutral", 0.9)),
        sizeUnits: "pixels",
        pickable: pick,
        onHover: (i) => this.hover(i.object ? `${(i.object as { row: Sea }).row.name ?? `MMSI ${(i.object as { row: Sea }).row.mmsi}`} · ${fmtSpeed((i.object as { row: Sea }).row.sog)}` : null, i.x, i.y),
        onClick: (i) => i.object && this.select({ kind: "sea", id: (i.object as { row: Sea }).row.mmsi }),
      }),
      new IconLayer<{ row: Air; n: number }>({
        id: "aircraft",
        data: da,
        iconAtlas: this.icons,
        iconMapping: MAPPING,
        getIcon: (d) => (d.row.mil ? "diamond" : "plane"),
        getPosition: (d) => [d.row.lon, d.row.lat, this.z(d.row)],
        getAngle: (d) => -(d.row.track ?? 0),
        updateTriggers: { getPosition: [view.mode, view.exaggeration] },
        getSize: (d) => (d.row.mil ? 17 : 16),
        getColor: (d) => (isMil(d.row) ? rgba("military", 1) : rgba("neutral", 0.95)),
        sizeUnits: "pixels",
        pickable: pick,
        onHover: (i) => {
          const r = (i.object as { row: Air } | undefined)?.row;
          this.hover(r ? `${r.flight ?? r.hex} · ${fmtAlt(r)}` : null, i.x, i.y);
        },
        onClick: (i) => i.object && this.select({ kind: "air", id: (i.object as { row: Air }).row.hex }),
      }),
      ...(view.mode === "3d" && view.drop
        ? [
            new PathLayer<[number, number, number][]>({
              id: "aircraft-drop",
              data: dropLines(da.map((d) => d.row), true, view.exaggeration),
              getPath: (d) => d,
              getColor: rgba("neutral", 0.5),
              widthMinPixels: 1,
            }),
          ]
        : []),
      // thin amber ring: GPS accuracy problem on that aircraft
      new ScatterplotLayer<{ row: Air }>({
        id: "gps-ring",
        data: da.filter((d) => d.row.gps_bad),
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
        data: [...da, ...ds].filter((d) => d.row.in_alert),
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
              ...(air.mil ? ([[T.military, T.yes]] as [string, string][]) : []),
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
        h("div", { class: "row" }, follow, rel, iconButton("close", T.closeCard, () => closeWindow("entity"))),
      ),
    );
  }

  /** Clear the picked entity from outside (Esc handling is done by the window manager). */
  dispose(): void {
    this.timers.forEach((t) => window.clearInterval(t));
  }
}
