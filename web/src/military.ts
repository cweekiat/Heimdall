// Military air activity (task 039): worldwide military aircraft seen by the ADS-B networks, with identity from
// plane-alert-db (community list, ODbL). Contacts, roles, types, countries, high-interest aircraft, theatres and a
// searchable list; clicking an aircraft or theatre flies the map there. Build-up alerts arrive in the alert queue.
// All text via textContent (h helper): operator names and tags are community data, never markup.
import { api } from "./api";
import type { MilAircraft, MilitarySummary } from "./api";
import { h } from "./dom";
import { fmtHM } from "./tz";
import { createWindow, EDGE_LEFT, EDGE_TOP } from "./wm";

export const ROLE_LABEL: Record<string, string> = {
  tanker: "Tankers",
  bomber: "Bombers",
  isr: "Surveillance",
  c2: "Command posts",
  airlift: "Transport",
  fighter: "Fighters",
  special: "Special forces",
  uav: "Drones",
  rotary: "Helicopters",
  gov_vip: "Government / VIP",
  trainer: "Trainers",
  other: "Other",
};
/** Roles that matter most for build-ups first. */
export const ROLE_ORDER = ["tanker", "bomber", "isr", "c2", "airlift", "special", "fighter", "uav", "gov_vip", "rotary", "trainer", "other"];

export function aircraftLine(a: MilAircraft): string {
  const id = a.flight || a.registration || a.hex.toUpperCase();
  const what = a.type_name || a.type || "unknown type";
  const alt = a.on_ground ? "on ground" : a.alt !== null ? `${Math.round(a.alt / 100) * 100} ft` : "";
  return [id, what, a.operator || a.country || "", alt].filter(Boolean).join(" · ");
}

export function matches(a: MilAircraft, q: string): boolean {
  if (!q) return true;
  const s = q.toLowerCase();
  return [a.hex, a.flight, a.registration, a.type, a.type_name, a.operator, a.country, a.category, ROLE_LABEL[a.role]].some(
    (v) => typeof v === "string" && v.toLowerCase().includes(s),
  );
}

export interface MilitaryHooks {
  flyTo: (lon: number, lat: number) => void;
}

export class MilitaryPanel {
  readonly body: HTMLElement;
  data: MilitarySummary | null = null;
  private error = "";
  private query = "";
  private role: string | null = null;

  constructor(private hooks: MilitaryHooks) {
    this.body = createWindow({ id: "military", title: "Military air activity", icon: "plane", x: EDGE_LEFT + 8, y: EDGE_TOP + 8, w: 380, h: 640, minW: 300 });
    this.render();
  }

  start(): void {
    void this.refresh();
    window.setInterval(() => void this.refresh(), 30_000);
  }

  async refresh(): Promise<void> {
    try {
      this.data = await api.military();
      this.error = "";
    } catch (e) {
      this.error = `Military data unavailable (${String(e).slice(0, 60)})`;
    }
    this.render();
  }

  private bars(entries: [string, string, number][], onPick?: (key: string) => void): HTMLElement {
    const max = Math.max(1, ...entries.map((e) => e[2]));
    return h(
      "div",
      { class: "mil-bars" },
      ...entries.map(([key, label, n]) =>
        h(
          onPick ? "button" : "div",
          {
            class: "mil-bar",
            ...(onPick ? { type: "button", "aria-pressed": String(this.role === key), onclick: () => onPick(key) } : {}),
          },
          h("span", { class: "mil-bar-fill", style: `width:${Math.round((100 * n) / max)}%` }),
          h("span", { class: "mil-bar-label" }, label),
          h("span", { class: "mil-bar-n" }, String(n)),
        ),
      ),
    );
  }

  private row(a: MilAircraft): HTMLElement {
    return h(
      "button",
      { type: "button", class: `mil-ac${a.high_interest ? " hi" : ""}`, title: a.tags.join(", "), onclick: () => this.hooks.flyTo(a.lon, a.lat) },
      h("span", { class: `mil-role r-${a.role}` }, ROLE_LABEL[a.role] ?? a.role),
      h("span", { class: "mil-line" }, aircraftLine(a)),
      a.category ? h("span", { class: "mil-cat" }, a.category) : null,
    );
  }

  render(): void {
    const d = this.data;
    if (!d) {
      this.body.replaceChildren(h("div", { class: "empty" }, this.error || "Loading military aircraft…"));
      return;
    }
    const roles = ROLE_ORDER.filter((r) => d.roles[r]).map((r): [string, string, number] => [r, ROLE_LABEL[r] ?? r, d.roles[r]!]);
    const list = d.aircraft.filter((a) => (this.role === null || a.role === this.role) && matches(a, this.query));
    const search = h("input", {
      type: "search",
      class: "mil-search",
      placeholder: "Callsign, type, registration, operator",
      value: this.query,
      "aria-label": "Search military aircraft",
      oninput: (ev: Event) => {
        this.query = (ev.target as HTMLInputElement).value;
        const pos = (ev.target as HTMLInputElement).selectionStart;
        this.render();
        const el = this.body.querySelector<HTMLInputElement>(".mil-search");
        el?.focus();
        if (pos !== null) el?.setSelectionRange(pos, pos);
      },
    });
    const parts: (HTMLElement | null)[] = [
      this.error ? h("div", { class: "note warn" }, this.error) : null,
      h(
        "div",
        { class: "mil-stats" },
        h("div", {}, h("b", {}, String(d.contacts)), h("span", {}, "contacts")),
        h("div", {}, h("b", {}, String(d.airborne)), h("span", {}, "airborne")),
        h("div", {}, h("b", {}, String(d.on_ground)), h("span", {}, "on ground")),
        h("div", {}, h("b", {}, String(d.identified)), h("span", {}, "identified")),
      ),
      d.high_interest.length
        ? h("section", { class: "mil-sec" }, h("h4", {}, `High interest (${d.high_interest.length})`), ...d.high_interest.map((a) => this.row(a)))
        : null,
      h("section", { class: "mil-sec" }, h("h4", {}, "Roles"), this.bars(roles, (r) => {
        this.role = this.role === r ? null : r;
        this.render();
      })),
      h(
        "section",
        { class: "mil-sec" },
        h("h4", {}, "Theatres"),
        ...d.theatres
          .filter((t) => t.n > 0)
          .sort((a, b) => b.n - a.n)
          .map((t) =>
            h(
              "button",
              { type: "button", class: "mil-th", onclick: () => this.hooks.flyTo(t.lon, t.lat) },
              h("span", {}, t.name),
              h("span", { class: "mil-th-roles" }, ROLE_ORDER.filter((r) => t.roles[r]).slice(0, 4).map((r) => `${ROLE_LABEL[r]} ${t.roles[r]}`).join(" · ")),
              h("b", {}, String(t.n)),
            ),
          ),
      ),
      h("section", { class: "mil-sec" }, h("h4", {}, "Types"), this.bars(d.types.slice(0, 8).map((t): [string, string, number] => [t.type, t.name ? `${t.type} · ${t.name}` : t.type, t.n]))),
      h("section", { class: "mil-sec" }, h("h4", {}, "Operator country"), this.bars(d.countries.slice(0, 8).map((c): [string, string, number] => [c.country, c.country, c.n]))),
      h(
        "section",
        { class: "mil-sec" },
        h("h4", {}, `Aircraft (${list.length}${this.role ? ` · ${ROLE_LABEL[this.role]}` : ""})`),
        search,
        ...list.slice(0, 200).map((a) => this.row(a)),
      ),
      h("div", { class: "note" }, `Updated ${fmtHM(d.generated)} · only aircraft broadcasting ADS-B and flagged military are seen. ${d.attribution}.`),
    ];
    this.body.replaceChildren(...parts.filter((p): p is HTMLElement => p !== null));
  }
}
