// Military aircraft list window (task 046, IntelSky-style; rail "List", key y). Worldwide military contacts from
// GET /api/military with All / In view / Selected tabs, sortable columns, text filters, role chips shared with the
// map filter, multi-select, "Hide others" (map shows only the picked aircraft) and CSV / KML export.
// All community text goes in via textContent (h helper).
import { api } from "./api";
import type { MilAircraft } from "./api";
import { EMPTY_QUERY, ident, selectRows, sortRows, toCsv, toKml } from "./aclist";
import type { ListQuery, SortKey, Tab } from "./aclist";
import { h } from "./dom";
import { filters, onFilters, setFilter } from "./filters";
import { ROLE_LABEL, ROLE_ORDER, roleCss } from "./milroles";
import { fmtHM } from "./tz";
import { createWindow, EDGE_LEFT, EDGE_TOP, isOpen } from "./wm";

export interface ListHooks {
  bbox: () => string;
  flyTo: (lon: number, lat: number) => void;
  /** Open the aircraft investigation; it flies the map there itself. */
  pick: (hex: string) => void;
}

const COLS: { key: SortKey; label: string }[] = [
  { key: "ident", label: "Ident / hex" },
  { key: "type", label: "Type" },
  { key: "operator", label: "Operator" },
  { key: "role", label: "Role" },
  { key: "alt", label: "Alt ft" },
  { key: "gs", label: "Kt" },
];

function download(name: string, mime: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = h("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export class AircraftList {
  readonly body: HTMLElement;
  private rows: MilAircraft[] = [];
  private updated = "";
  private error = "";
  private tab: Tab = "view";
  private sort: SortKey = "role";
  private desc = false;
  private q: ListQuery = { ...EMPTY_QUERY };
  private selected = new Set<string>();
  private table = h("div", { class: "acl-table" });
  private tabsEl = h("div", { class: "acl-tabs", role: "tablist" });
  private foot = h("div", { class: "acl-foot" });
  private chips = h("div", { class: "chips" });

  constructor(private hooks: ListHooks) {
    this.body = createWindow({ id: "aclist", title: "Aircraft list", icon: "plane", x: EDGE_LEFT + 8, y: EDGE_TOP + 8, w: 560, h: 620, minW: 380 });
    const field = (key: keyof Omit<ListQuery, "roles">, label: string, ph: string): HTMLElement => {
      const i = h("input", { type: "search", placeholder: ph, "aria-label": label });
      i.addEventListener("input", () => {
        this.q = { ...this.q, [key]: i.value };
        this.draw();
      });
      return h("label", { class: "acl-field" }, h("span", {}, label), i);
    };
    this.body.replaceChildren(
      h(
        "div",
        { class: "acl" },
        h(
          "div",
          { class: "acl-filters" },
          field("callsign", "Callsign", "e.g. RCH123"),
          field("type", "Type", "e.g. C17 or Globemaster"),
          field("hex", "Hex", "e.g. AE1234"),
          field("operator", "Operator / country", "e.g. USAF"),
        ),
        this.chips,
        this.tabsEl,
        this.table,
        this.foot,
      ),
    );
    onFilters(() => this.draw());
    this.draw();
  }

  start(): void {
    void this.refresh();
    window.setInterval(() => isOpen("aclist") && void this.refresh(), 20_000);
  }

  async refresh(): Promise<void> {
    try {
      const d = await api.military();
      this.rows = d.aircraft;
      this.updated = d.generated;
      this.error = "";
    } catch (e) {
      this.error = `Military data unavailable (${String(e).slice(0, 60)})`;
    }
    this.draw();
  }

  private query(): ListQuery {
    return { ...this.q, roles: filters.milRoles };
  }

  draw(): void {
    const q = this.query();
    const bbox = this.hooks.bbox();
    const count = (t: Tab): number => selectRows(this.rows, t, q, bbox, this.selected).length;
    this.chips.replaceChildren(
      ...ROLE_ORDER.map((r) => {
        const on = filters.milRoles.includes(r);
        return h(
          "button",
          { type: "button", class: "chip role-chip", "aria-pressed": String(on), onclick: () => setFilter({ milRoles: on ? filters.milRoles.filter((x) => x !== r) : [...filters.milRoles, r] }) },
          h("i", { class: "role-dot", style: `background:${roleCss(r)}` }),
          ROLE_LABEL[r] ?? r,
        );
      }),
    );
    this.tabsEl.replaceChildren(
      ...(
        [
          ["all", "All"],
          ["view", "In view"],
          ["selected", "Selected"],
        ] as [Tab, string][]
      ).map(([t, label]) =>
        h("button", { type: "button", role: "tab", class: "acl-tab", "aria-selected": String(this.tab === t), onclick: () => ((this.tab = t), this.draw()) }, `${label} ${count(t)}`),
      ),
    );
    const list = sortRows(selectRows(this.rows, this.tab, q, bbox, this.selected), this.sort, this.desc);
    const head = h(
      "div",
      { class: "acl-row acl-head" },
      h("span", {}, ""),
      ...COLS.map((c) =>
        h(
          "button",
          {
            type: "button",
            class: "acl-sort",
            "aria-sort": this.sort === c.key ? (this.desc ? "descending" : "ascending") : "none",
            onclick: () => {
              this.desc = this.sort === c.key ? !this.desc : c.key === "alt" || c.key === "gs";
              this.sort = c.key;
              this.draw();
            },
          },
          c.label,
          this.sort === c.key ? (this.desc ? " ▾" : " ▴") : "",
        ),
      ),
    );
    const row = (a: MilAircraft): HTMLElement => {
      const cb = h("input", { type: "checkbox", "aria-label": `Select ${ident(a)}` });
      cb.checked = this.selected.has(a.hex);
      cb.addEventListener("change", () => {
        if (cb.checked) this.selected.add(a.hex);
        else this.selected.delete(a.hex);
        this.draw();
      });
      return h(
        "div",
        { class: `acl-row${a.high_interest ? " hi" : ""}` },
        cb,
        h(
          "button",
          { type: "button", class: "acl-id", title: "Investigate", onclick: () => this.hooks.pick(a.hex) },
          h("b", {}, ident(a)),
          h("small", {}, a.hex.toUpperCase()),
        ),
        h("span", { class: "acl-type" }, h("b", {}, a.type ?? "?"), h("small", {}, a.type_name ?? "")),
        h("span", { class: "acl-op" }, h("span", {}, a.operator ?? "—"), h("small", {}, a.country ?? "")),
        h("span", { class: `mil-role r-${a.role}`, style: `color:${roleCss(a.role)}` }, ROLE_LABEL[a.role] ?? a.role),
        h("span", { class: "num" }, a.on_ground ? "ground" : a.alt !== null ? String(Math.round(a.alt / 100) * 100) : "—"),
        h("span", { class: "num" }, a.gs !== null ? String(Math.round(a.gs)) : "—"),
      );
    };
    this.table.replaceChildren(
      head,
      ...(list.length
        ? list.slice(0, 400).map(row)
        : [h("div", { class: "empty" }, this.error || (this.rows.length ? "No aircraft match." : "Loading military aircraft…"))]),
      ...(list.length > 400 ? [h("div", { class: "note" }, `First 400 of ${list.length}; narrow the filters.`)] : []),
    );
    const hiding = filters.onlyHexes.length > 0;
    this.foot.replaceChildren(
      h("span", { class: "note" }, `${this.selected.size} selected`),
      h(
        "button",
        {
          type: "button",
          disabled: !hiding && this.selected.size === 0,
          onclick: () => setFilter({ onlyHexes: hiding ? [] : [...this.selected].map((x) => x.toLowerCase()) }),
        },
        hiding ? "Show others" : "Hide others",
      ),
      h("button", { type: "button", disabled: this.selected.size === 0, onclick: () => (this.selected.clear(), this.draw()) }, "Clear"),
      h("button", { type: "button", disabled: !list.length, onclick: () => download("heimdall-military.csv", "text/csv", toCsv(list)) }, "CSV"),
      h("button", { type: "button", disabled: !list.length, onclick: () => download("heimdall-military.kml", "application/vnd.google-earth.kml+xml", toKml(list)) }, "KML"),
      h("span", { class: "note" }, this.updated ? `Updated ${fmtHM(this.updated)} · ADS-B military only` : ""),
    );
  }
}
