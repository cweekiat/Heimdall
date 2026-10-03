// Aircraft investigation window (task 047): click a military aircraft (map card, aircraft list, Military panel) to
// see what it is, whose it is, what the type can do, where it took off and what it may be doing now, with the
// reasons. Draws its 72 h track coloured by altitude and the take-off airfield. Community text via textContent.
import { PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { api } from "./api";
import type { Investigation } from "./api";
import { h } from "./dom";
import { ALT_STOPS, CONF_WORD, refLines, trackSegments } from "./investigatemodel";
import type { Segment } from "./investigatemodel";
import { ROLE_LABEL, roleCss } from "./milroles";
import { fmtHM } from "./tz";
import { closeWindow, createWindow, EDGE_TOP, openWindow, setTitle } from "./wm";

export interface InvestigateHooks {
  flyTo: (lon: number, lat: number) => void;
  render: () => void;
}

const kv = (rows: [string, string | Node][]): HTMLElement => h("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));

export class Investigator {
  readonly body: HTMLElement;
  private data: Investigation | null = null;
  private segs: Segment[] = [];
  private hex: string | null = null;
  private timer = 0;

  constructor(private hooks: InvestigateHooks) {
    this.body = createWindow({
      id: "investigate",
      title: "Aircraft investigation",
      icon: "plane",
      x: -40,
      y: EDGE_TOP + 8,
      w: 400,
      h: 680,
      minW: 320,
      onClose: () => {
        this.hex = null;
        this.data = null;
        this.segs = [];
        window.clearInterval(this.timer);
        this.hooks.render();
      },
    });
  }

  /** Open for one aircraft (ICAO hex) and refresh every 30 s while open. */
  open(hex: string): void {
    this.hex = hex.toLowerCase();
    this.data = null;
    this.segs = [];
    this.body.replaceChildren(h("div", { class: "empty" }, "Investigating…"));
    openWindow("investigate");
    void this.load(true);
    window.clearInterval(this.timer);
    this.timer = window.setInterval(() => void this.load(false), 30_000);
  }

  private async load(fly: boolean): Promise<void> {
    const hex = this.hex;
    if (!hex) return;
    try {
      const d = await api.investigate(hex);
      if (hex !== this.hex) return;
      this.data = d;
      this.segs = trackSegments(d);
      this.render();
      if (fly) this.hooks.flyTo(d.last.lon, d.last.lat);
    } catch (e) {
      if (hex !== this.hex) return;
      this.body.replaceChildren(h("div", { class: "empty" }, `No recent positions for ${hex.toUpperCase()} (${String(e).slice(0, 60)}).`));
    }
    this.hooks.render();
  }

  private render(): void {
    const d = this.data;
    if (!d) return;
    const id = d.flight || d.registration || d.hex.toUpperCase();
    setTitle("investigate", `Investigate ${id}`);
    const role = ROLE_LABEL[d.role] ?? d.role;
    const ref = refLines(d.reference);
    const dep = d.departure;
    this.body.replaceChildren(
      h(
        "div",
        { class: "body inv" },
        h(
          "div",
          { class: "inv-head" },
          h("h2", {}, id),
          h("span", { class: "note" }, `${d.hex.toUpperCase()}${d.registration && d.registration !== id ? ` · ${d.registration}` : ""}`),
          h("span", { class: `mil-role r-${d.role}`, style: `color:${roleCss(d.role)}` }, role),
          d.high_interest ? h("span", { class: "badge warn" }, "High interest") : null,
        ),
        h(
          "div",
          { class: "inv-tiles" },
          h("div", {}, h("span", {}, "Altitude"), h("b", {}, d.last.alt === null ? "ground / n.a." : `${Math.round(d.last.alt).toLocaleString("en")} ft`)),
          h("div", {}, h("span", {}, "Speed"), h("b", {}, d.last.gs === null ? "n.a." : `${Math.round(d.last.gs)} kt`)),
          h("div", {}, h("span", {}, "Last seen"), h("b", {}, fmtHM(d.last.ts))),
        ),
        h(
          "section",
          { class: `inv-now ${d.activity.confidence}` },
          h("h4", {}, "What it may be doing now"),
          h("p", { class: "inv-label" }, h("span", { class: "badge" }, CONF_WORD[d.activity.confidence] ?? d.activity.confidence), " ", d.activity.label),
          ...d.activity.reasons.map((r) => h("p", { class: "note" }, r)),
          ...d.activity.context.map((c) => h("p", { class: "inv-ctx" }, c)),
        ),
        h(
          "section",
          {},
          h("h4", {}, dep.kind === "took_off" ? "Took off from" : dep.kind === "on_ground" ? "On the ground" : "Origin"),
          h("p", {}, dep.text),
          dep.airfield ? h("p", { class: "note" }, `${dep.airfield.military ? "Military airfield" : "Airfield"} · ${dep.airfield.kind.replace("_", " ")} · ${dep.airfield.distance_km} km from the first point`) : null,
          d.airfields_loaded ? null : h("p", { class: "note warn" }, "Airfield list not downloaded yet (Data sources → Airfields)."),
        ),
        h(
          "section",
          {},
          h("h4", {}, "Identity"),
          kv([
            ["Type", [d.type, d.type_name ?? d.reference?.name].filter(Boolean).join(" · ") || "unknown"],
            ["Operator", d.operator ?? "unknown"],
            ["Country", d.country ?? "unknown"],
            ...(d.category ? ([["Listed as", d.category]] as [string, string][]) : []),
            ...(d.tags.length ? ([["Tags", d.tags.join(", ")]] as [string, string][]) : []),
          ]),
          d.known ? null : h("p", { class: "note" }, "Not in plane-alert-db: identity from the type code and address block only."),
        ),
        h(
          "section",
          {},
          h("h4", {}, "Type capabilities (reference)"),
          ref.length ? kv(ref) : h("p", { class: "note" }, "No reference entry for this type."),
          d.reference?.source ? h("p", { class: "note" }, "Source: ", h("a", { href: d.reference.source, target: "_blank", rel: "noopener noreferrer" }, "Wikipedia")) : null,
        ),
        h(
          "div",
          { class: "inv-key" },
          h("span", { class: "note" }, "Track altitude"),
          h("i", { class: "inv-ramp", style: `background:linear-gradient(90deg, ${ALT_STOPS.map(([a, c]) => `rgb(${c.join(",")}) ${(a / 40000) * 100}%`).join(", ")})` }),
          h("span", { class: "note" }, "0 · 10k · 20k · 30k · 40k ft · faded = earlier flights"),
        ),
        h("p", { class: "note" }, d.limits, " ", d.attribution, "."),
        h(
          "div",
          { class: "row" },
          h("button", { type: "button", onclick: () => this.hooks.flyTo(d.last.lon, d.last.lat) }, "Show on map"),
          h("button", { type: "button", onclick: () => void this.load(false) }, "Refresh"),
          h("button", { type: "button", onclick: () => closeWindow("investigate") }, "Close"),
        ),
      ),
    );
  }

  layers(): unknown[] {
    const d = this.data;
    if (!d) return [];
    const dep = d.departure.airfield;
    return [
      new PathLayer<Segment>({ id: "inv-track", data: this.segs, getPath: (s) => s.path, getColor: (s) => s.color, widthMinPixels: 3, capRounded: true }),
      new ScatterplotLayer<{ p: [number, number] }>({
        id: "inv-now",
        data: [{ p: [d.last.lon, d.last.lat] }],
        getPosition: (x) => x.p,
        getFillColor: [0, 0, 0, 0],
        getLineColor: [255, 176, 32, 255],
        stroked: true,
        lineWidthMinPixels: 2,
        radiusMinPixels: 12,
        getRadius: 1,
      }),
      ...(dep
        ? [
            new ScatterplotLayer<{ p: [number, number] }>({ id: "inv-dep", data: [{ p: [dep.lon, dep.lat] }], getPosition: (x) => x.p, getFillColor: [255, 255, 255, 230], getLineColor: [40, 40, 40, 255], stroked: true, lineWidthMinPixels: 1, radiusMinPixels: 5, getRadius: 1 }),
            new TextLayer<{ p: [number, number]; t: string }>({
              id: "inv-dep-label",
              data: [{ p: [dep.lon, dep.lat], t: dep.ident || dep.name }],
              getPosition: (x) => x.p,
              getText: (x) => x.t,
              getSize: 12,
              getPixelOffset: [0, -14],
              getColor: [255, 255, 255, 240],
              outlineWidth: 2,
              outlineColor: [0, 0, 0, 220],
              fontSettings: { sdf: true },
            }),
          ]
        : []),
    ];
  }
}
