// Settings page: theme, map overlays and online map styles, window layout, guided tour.
import { h } from "./dom";
import { T } from "./labels";
import { startTour } from "./tour/overlay";
import { COMMON_TZ, getTz, setTz, tzOffset } from "./tz";
import { createWindow, EDGE_LEFT, EDGE_TOP, closeWindow, resetLayout } from "./wm";

export const THEMES = ["dark", "light", "hc"] as const;
export type Theme = (typeof THEMES)[number];
const THEME_LABEL: Record<Theme, string> = { dark: "Dark", light: "Light", hc: "High contrast" };

export interface SettingsCtx {
  theme: () => Theme;
  setTheme: (t: Theme) => void;
  /** Overlays + "allow online map styles" block (from the base-map manager). */
  mapPanel: () => HTMLElement | null;
}

export class SettingsPage {
  private body: HTMLElement;

  constructor(private ctx: SettingsCtx) {
    this.body = createWindow({
      id: "settings",
      title: "Settings",
      icon: "settings",
      x: EDGE_LEFT + 8,
      y: EDGE_TOP + 8,
      w: 380,
      h: 560,
      onOpen: () => this.render(),
    });
  }

  render(): void {
    const themes = h(
      "fieldset",
      {},
      h("legend", {}, "Theme"),
      ...THEMES.map((t) =>
        h(
          "label",
          { class: "chk" },
          h("input", { type: "radio", name: "hm-theme", checked: this.ctx.theme() === t, onchange: () => this.ctx.setTheme(t) }),
          THEME_LABEL[t],
        ),
      ),
    );
    const all = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
    const opt = (z: string): HTMLOptionElement => {
      const o = h("option", { value: z }, `${z.replace(/_/g, " ")} (${tzOffset(z)})`);
      o.selected = z === getTz();
      return o;
    };
    const tzSel = h(
      "select",
      { "aria-label": "Time zone", onchange: (e: Event) => setTz((e.target as HTMLSelectElement).value) },
      h("optgroup", { label: "Common" }, ...COMMON_TZ.map(opt)),
      h("optgroup", { label: "All zones" }, ...all.filter((z) => !COMMON_TZ.includes(z)).map(opt)),
    );
    const timeBox = h(
      "fieldset",
      {},
      h("legend", {}, "Time zone"),
      tzSel,
      h("p", { class: "note" }, "All times on screen use this zone (default Singapore, UTC+8). Data is stored in UTC; daily GNSS maps are UTC days."),
    );
    const map = this.ctx.mapPanel();
    this.body.replaceChildren(
      h(
        "div",
        { class: "body settings" },
        themes,
        timeBox,
        map ? h("div", { class: "settings-map" }, map) : h("p", { class: "note" }, "Map settings appear once the map has loaded."),
        h(
          "fieldset",
          {},
          h("legend", {}, "Map view"),
          h("p", { class: "note" }, "Zoom out past the whole region and the map turns into a globe; zoom in and it flattens again. The 3D / 2D button (bottom right) switches directly, and the target button returns to South-East Asia."),
        ),
        h(
          "fieldset",
          {},
          h("legend", {}, "Windows"),
          h("button", { type: "button", onclick: () => resetLayout() }, T.resetLayout),
          h("p", { class: "note" }, "Puts every window back to its default place and size."),
        ),
        h(
          "fieldset",
          {},
          h("legend", {}, "Help"),
          h("button", { type: "button", onclick: () => { closeWindow("settings"); startTour(); } }, "Take the guided tour again"),
        ),
        h("p", { class: "note" }, "API keys and source on/off switches are in Data sources."),
      ),
    );
  }
}
