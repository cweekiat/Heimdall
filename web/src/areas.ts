// "Draw areas" window (task 015): draw, name, select, show/hide, monitor on/off and delete areas of operation.
// Config areas are read-only ("config"). The server validates everything again; errors are shown as plain text.
import { api } from "./api";
import type { Aoi, UserAoi } from "./api";
import { circleProblem, circleRing, MAX_USER_AREAS, nameProblem, polygonProblem, polygonRing } from "./aogeo";
import type { DisplayAo } from "./aogeo";
import { h } from "./dom";
import type { DrawEvent, DrawMode, DrawResult } from "./map";
import { T } from "./labels";
import { createWindow, EDGE_TOP, isOpen } from "./wm";

/** The slice of the map this window needs (the map module is loaded lazily). */
export interface Drawer {
  startDraw(mode: DrawMode): void;
  cancelDraw(): void;
  undoPoint(): void;
  finishPolygon(): void;
  flyTo(lon: number, lat: number): void;
  onDraw: (e: DrawEvent) => void;
}

const HIDDEN_KEY = "hm.ao.hidden";

export class Areas {
  user: UserAoi[] = [];
  selected: string | null = null;
  /** Repaint the map outlines. */
  onChange: () => void = () => undefined;
  /** The set of monitored areas changed on the server: refetch areas, alerts and traffic. */
  onServerChange: () => void = () => undefined;
  private body: HTMLElement;
  private hidden = new Set<string>();
  private drawer: Drawer | null = null;
  private drawing: { mode: DrawMode; text: string; ready: boolean } | null = null;
  private draft: DrawResult | null = null;
  private renaming: string | null = null;
  private confirm: string | null = null;
  private error = "";
  private busy = false;

  constructor(private configAois: () => Aoi[]) {
    try {
      this.hidden = new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) ?? "[]") as string[]);
    } catch {
      this.hidden = new Set();
    }
    this.body = createWindow({
      id: "draw",
      title: T.drawAreas,
      icon: "draw",
      x: -60,
      y: EDGE_TOP + 8,
      w: 380,
      h: 540,
      onOpen: () => {
        void this.reload();
        this.render();
      },
      onClose: () => {
        this.drawer?.cancelDraw();
        this.draft = null;
      },
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.drawing) this.drawer?.cancelDraw();
    });
    this.render();
  }

  attach(d: Drawer): void {
    this.drawer = d;
    d.onDraw = (e) => this.onDraw(e);
  }

  async reload(): Promise<void> {
    try {
      this.user = await api.userAois();
      this.error = "";
    } catch (e) {
      this.error = String(e);
    }
    this.changed();
  }

  /** Outlines for the map: config areas plus user areas, minus hidden ones. */
  display(): DisplayAo[] {
    const out: DisplayAo[] = [];
    for (const a of this.configAois()) {
      if (a.origin === "user" || this.hidden.has(a.name)) continue;
      out.push({ id: a.name, ring: circleRing(a.lat, a.lon, a.radius_nm), role: a.role, origin: "config", active: true, selected: a.name === this.selected });
    }
    for (const u of this.user) {
      if (this.hidden.has(u.id)) continue;
      const ring = u.polygon ? polygonRing(u.polygon) : circleRing(u.lat, u.lon, u.radius_nm);
      out.push({ id: u.id, ring, role: u.role, origin: "user", active: u.active, selected: u.id === this.selected });
    }
    return out;
  }

  private changed(): void {
    this.onChange();
    if (isOpen("draw")) this.render();
  }

  private onDraw(e: DrawEvent): void {
    if (e.kind === "progress") this.drawing = { mode: this.drawing?.mode ?? "polygon", text: e.text, ready: e.ready };
    else {
      this.drawing = null;
      if (e.kind === "done") this.draft = e.result;
    }
    this.render();
  }

  private start(mode: DrawMode): void {
    this.error = "";
    this.draft = null;
    this.drawing = { mode, text: "", ready: false };
    this.drawer?.startDraw(mode);
  }

  private async run(fn: () => Promise<unknown>, serverChange = true): Promise<boolean> {
    if (this.busy) return false;
    this.busy = true;
    try {
      await fn();
      this.error = "";
      this.user = await api.userAois();
      if (serverChange) this.onServerChange();
      return true;
    } catch (e) {
      this.error = String(e).replace(/^Error:\s*/, "");
      return false;
    } finally {
      this.busy = false;
      this.changed();
    }
  }

  private toggleHidden(id: string, show: boolean): void {
    if (show) this.hidden.delete(id);
    else this.hidden.add(id);
    try {
      localStorage.setItem(HIDDEN_KEY, JSON.stringify([...this.hidden]));
    } catch {
      /* private mode: the choice lasts until reload */
    }
    this.changed();
  }

  private select(id: string, lon: number, lat: number): void {
    this.selected = this.selected === id ? null : id;
    if (this.selected) this.drawer?.flyTo(lon, lat);
    this.changed();
  }

  private form(r: DrawResult): HTMLElement {
    const name = h("input", { type: "text", maxlength: "40", "aria-label": "Area name", placeholder: "Name (letters, numbers, spaces)" });
    const role = h("select", { "aria-label": "Role" }, h("option", { value: "primary" }, "Primary"), h("option", { value: "control" }, "Control (for comparison)"));
    const msg = h("div", { class: "fail", role: "alert", hidden: true });
    const summary = r.shape === "circle" ? `Circle, radius ${r.radius_nm} nautical miles` : `Polygon, ${r.polygon.length} points`;
    const save = (): void => {
      const problem = nameProblem(name.value) ?? (r.shape === "circle" ? circleProblem(r.lat, r.lon, r.radius_nm) : polygonProblem(r.polygon));
      if (problem) {
        msg.textContent = problem;
        msg.hidden = false;
        return;
      }
      const body = { name: name.value, role: role.value as "primary" | "control", ...r };
      void this.run(() => api.createAoi(body)).then((ok) => {
        if (ok) this.draft = null;
        this.render();
      });
    };
    return h(
      "fieldset",
      { class: "ao-form" },
      h("legend", {}, "Name this area"),
      h("p", { class: "note" }, summary),
      h("label", {}, "Name ", name),
      h("label", {}, "Role ", role),
      msg,
      h(
        "div",
        { class: "row" },
        h("button", { type: "button", class: "primary", onclick: save }, "Save area"),
        h("button", { type: "button", onclick: () => { this.draft = null; this.render(); } }, "Discard"),
      ),
    );
  }

  private row(u: { id: string; label: string; origin: "config" | "user"; role: string; active: boolean; lat: number; lon: number; detail: string }): HTMLElement {
    const shown = h("input", { type: "checkbox", "aria-label": `Show ${u.label} on the map` });
    shown.checked = !this.hidden.has(u.id);
    shown.addEventListener("change", () => this.toggleHidden(u.id, shown.checked));
    const nameEl =
      this.renaming === u.id
        ? (() => {
            const inp = h("input", { type: "text", maxlength: "40", value: u.label, "aria-label": "New name" });
            const go = (): void => {
              const p = nameProblem(inp.value);
              if (p) {
                this.error = p;
                return this.render();
              }
              this.renaming = null;
              void this.run(() => api.patchAoi(u.id, { name: inp.value }));
            };
            inp.addEventListener("keydown", (e) => {
              if (e.key === "Enter") go();
              if (e.key === "Escape") {
                this.renaming = null;
                this.render();
              }
            });
            queueMicrotask(() => inp.focus());
            return h("span", { class: "ao-rename" }, inp, h("button", { type: "button", onclick: go }, "OK"));
          })()
        : h("button", { type: "button", class: "ao-name", "aria-pressed": String(this.selected === u.id), title: "Select and centre on the map", onclick: () => this.select(u.id, u.lon, u.lat) }, u.label);
    const tag = u.origin === "config" ? h("span", { class: "tag", title: "Set in the configuration file; read-only" }, "config") : h("span", { class: "tag" }, u.role);
    const controls: (HTMLElement | null)[] = [];
    if (u.origin === "user") {
      const mon = h("input", { type: "checkbox", role: "switch", "aria-label": `Monitor ${u.label}` });
      mon.checked = u.active;
      mon.addEventListener("change", () => void this.run(() => api.patchAoi(u.id, { active: mon.checked })));
      controls.push(
        h("label", { class: "sw", title: "Collect data and run detection here" }, mon, h("span", {}, u.active ? "Monitoring" : "Not monitored")),
        h("button", { type: "button", onclick: () => { this.renaming = u.id; this.render(); } }, "Rename"),
        h("button", { type: "button", class: "danger", onclick: () => { this.confirm = u.id; this.render(); } }, "Delete"),
      );
    }
    return h(
      "li",
      { class: `ao-row${this.selected === u.id ? " sel" : ""}` },
      h("div", { class: "ao-head" }, h("label", { class: "chk", title: "Show on the map" }, shown), nameEl, tag),
      h("div", { class: "note" }, u.detail),
      this.confirm === u.id
        ? h(
            "div",
            { class: "confirm", role: "alert" },
            h("span", {}, `Delete ${u.label}? Data collection there stops.`),
            h("button", { class: "danger", type: "button", onclick: () => { this.confirm = null; if (this.selected === u.id) this.selected = null; void this.run(() => api.deleteAoi(u.id)); } }, "Delete"),
            h("button", { type: "button", onclick: () => { this.confirm = null; this.render(); } }, "Keep"),
          )
        : h("div", { class: "ao-ctl" }, ...controls),
    );
  }

  render(): void {
    const full = this.user.length >= MAX_USER_AREAS;
    const dr = this.drawing;
    const rows = [
      ...this.configAois()
        .filter((a) => a.origin !== "user")
        .map((a) => this.row({ id: a.name, label: a.label ?? a.name, origin: "config", role: a.role, active: true, lat: a.lat, lon: a.lon, detail: `Circle, ${Math.round(a.radius_nm)} nm` })),
      ...this.user.map((u) =>
        this.row({ id: u.id, label: u.name, origin: "user", role: u.role, active: u.active, lat: u.lat, lon: u.lon, detail: u.polygon ? `Polygon, ${u.polygon.length} points` : `Circle, ${Math.round(u.radius_nm)} nm` }),
      ),
    ];
    this.body.replaceChildren(
      h(
        "div",
        { class: "body ao-body" },
        h("p", { class: "note" }, "Draw an area on the map, name it, and choose which areas to monitor. Monitored areas get data collection and alerts."),
        h(
          "div",
          { class: "row" },
          h("button", { type: "button", disabled: !!dr || !!this.draft || full || !this.drawer, onclick: () => this.start("circle") }, "Draw circle"),
          h("button", { type: "button", disabled: !!dr || !!this.draft || full || !this.drawer, onclick: () => this.start("polygon") }, "Draw polygon"),
        ),
        full ? h("p", { class: "note" }, `You have ${MAX_USER_AREAS} areas, the most allowed. Delete one to add another.`) : null,
        dr
          ? h(
              "div",
              { class: "ao-status", role: "status" },
              h("span", {}, dr.text || "Drawing..."),
              dr.mode === "polygon" ? h("button", { type: "button", onclick: () => this.drawer?.undoPoint() }, "Undo point") : null,
              dr.mode === "polygon" ? h("button", { type: "button", class: "primary", disabled: !dr.ready, onclick: () => this.drawer?.finishPolygon() }, "Finish") : null,
              h("button", { type: "button", onclick: () => this.drawer?.cancelDraw() }, "Cancel"),
            )
          : null,
        this.draft ? this.form(this.draft) : null,
        this.error ? h("div", { class: "fail", role: "alert" }, this.error) : null,
        h("ul", { class: "ao-list" }, ...rows),
      ),
    );
  }
}
