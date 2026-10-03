// Air force watch window (task 056, rail "Air forces", key a). One air force's visible fleet: where every aircraft
// is (home base, known overseas detachment, elsewhere at home, abroad, airborne), movements between countries,
// flights per day and rule-based notes. Click an aircraft to investigate it. Community text via textContent.
import { api } from "./api";
import type { AfAircraft, AirForcePicture } from "./api";
import { h } from "./dom";
import { fmtDT } from "./tz";
import { createWindow, EDGE_LEFT, EDGE_TOP, isOpen } from "./wm";

export interface AirForceHooks {
  investigate: (hex: string, at?: string) => void;
  flyTo: (lon: number, lat: number) => void;
}

const STATUS: Record<string, string> = {
  airborne: "Airborne",
  home: "At home base",
  detachment: "At overseas detachment",
  home_country: "Elsewhere at home",
  abroad: "Abroad",
  unknown: "Last seen airborne",
};
const ORDER = ["airborne", "abroad", "detachment", "home", "home_country", "unknown"];

export class AirForceWindow {
  readonly body: HTMLElement;
  private key = "rsaf";
  private days = 3;
  private data: AirForcePicture | null = null;
  private forces: { key: string; name: string; short: string }[] = [];
  private error = "";
  private loading = false;

  constructor(private hooks: AirForceHooks) {
    this.body = createWindow({ id: "airforces", title: "Air force watch", icon: "plane", x: EDGE_LEFT + 8, y: EDGE_TOP + 8, w: 460, h: 680, minW: 340 });
    this.render();
  }

  start(): void {
    void api.airForces().then((f) => ((this.forces = f), this.render())).catch(() => undefined);
    window.setInterval(() => isOpen("airforces") && void this.refresh(), 300_000);
  }

  async refresh(): Promise<void> {
    this.loading = true;
    this.render();
    try {
      this.data = await api.airForce(this.key, this.days);
      this.error = "";
    } catch (e) {
      this.error = `Air force data unavailable (${String(e).slice(0, 60)})`;
    }
    this.loading = false;
    this.render();
  }

  private where(a: AfAircraft): string {
    if (a.status === "airborne") return `airborne · ${fmtDT(a.last_seen)}`;
    const p = a.place;
    const at = p ? `${p.name}${p.country ? ` (${p.country})` : ""}` : `${a.lat.toFixed(1)}, ${a.lon.toFixed(1)}`;
    return `${at} · ${p ? "since" : "last seen"} ${fmtDT(a.since ?? a.last_seen)}`;
  }

  render(): void {
    const d = this.data;
    const sel = h(
      "select",
      { "aria-label": "Air force", onchange: (e: Event) => ((this.key = (e.target as HTMLSelectElement).value), void this.refresh()) },
      ...(this.forces.length ? this.forces : [{ key: "rsaf", name: "Republic of Singapore Air Force", short: "RSAF" }]).map((f) =>
        h("option", { value: f.key, selected: f.key === this.key }, `${f.short} · ${f.name}`),
      ),
    );
    const daysSel = h(
      "select",
      { "aria-label": "Days covered", onchange: (e: Event) => ((this.days = Number((e.target as HTMLSelectElement).value)), void this.refresh()) },
      ...[1, 3, 7].map((n) => h("option", { value: String(n), selected: n === this.days }, `Last ${n} day${n > 1 ? "s" : ""}`)),
    );
    const head = h("div", { class: "row" }, sel, daysSel, h("button", { type: "button", onclick: () => void this.refresh() }, this.loading ? "Loading…" : "Refresh"));
    if (!d) {
      this.body.replaceChildren(h("div", { class: "body af" }, head, h("p", { class: "note" }, this.error || (this.loading ? "Reading the fleet's positions (first load can take a few seconds)…" : "Pick an air force and press Refresh."))));
      return;
    }
    const groups = ORDER.filter((s) => d.aircraft.some((a) => a.status === s));
    const maxDay = Math.max(1, ...d.flights_per_day.map((x) => x.flights));
    this.body.replaceChildren(
      h(
        "div",
        { class: "body af" },
        head,
        this.error ? h("p", { class: "note warn" }, this.error) : null,
        h(
          "div",
          { class: "mil-stats" },
          h("div", {}, h("b", {}, String(d.seen)), h("span", {}, `seen of ${d.fleet_listed} listed`)),
          ...ORDER.filter((s) => d.status[s]).map((s) => h("div", {}, h("b", {}, String(d.status[s])), h("span", {}, STATUS[s]?.toLowerCase() ?? s))),
        ),
        d.notes.length
          ? h("section", { class: "mil-sec" }, h("h4", {}, "Notes"), ...d.notes.map((n) => h("p", { class: `af-note ${n.level}` }, n.text)))
          : h("p", { class: "note" }, "Nothing notable: no aircraft away from home bases and known detachments, no busier-than-usual day."),
        ...groups.map((g) =>
          h(
            "section",
            { class: "mil-sec" },
            h("h4", {}, `${STATUS[g] ?? g} (${d.aircraft.filter((a) => a.status === g).length})`),
            ...d.aircraft
              .filter((a) => a.status === g)
              .map((a) =>
                h(
                  "button",
                  { type: "button", class: "af-ac", title: "Investigate this aircraft", onclick: () => this.hooks.investigate(a.hex, a.status === "airborne" ? undefined : a.last_seen) },
                  h("b", {}, a.ident),
                  h("span", {}, a.type_name || a.type || "?"),
                  h("small", {}, `${a.hex.toUpperCase()} · ${this.where(a)}`), // callsigns are reused across airframes
                ),
              ),
          ),
        ),
        h(
          "section",
          { class: "mil-sec" },
          h("h4", {}, `Movements between countries (${d.movements.length})`),
          ...(d.movements.length
            ? d.movements.map((m) => h("p", { class: "af-move" }, `${fmtDT(m.dep_t)} ${m.dep?.name ?? "?"} (${m.dep?.country ?? "?"}) → ${m.arr?.name ?? "?"} (${m.arr?.country ?? "?"}) · ${m.hex.toUpperCase()}`))
            : [h("p", { class: "note" }, "None in the period.")]),
        ),
        h(
          "section",
          { class: "mil-sec" },
          h("h4", {}, "Flights per day (UTC)"),
          h(
            "div",
            { class: "mil-bars" },
            ...d.flights_per_day.map((x) =>
              h("div", { class: "mil-bar" }, h("span", { class: "mil-bar-fill", style: `width:${Math.round((100 * x.flights) / maxDay)}%` }), h("span", { class: "mil-bar-label" }, x.day), h("span", { class: "mil-bar-n" }, String(x.flights))),
            ),
          ),
        ),
        d.not_seen.length
          ? h(
              "details",
              {},
              h("summary", {}, `Listed but not seen in the period (${d.not_seen.length})`),
              h("p", { class: "note" }, d.not_seen.map((n) => `${n.registration ?? n.hex.toUpperCase()} ${n.type ?? ""}`.trim()).join(" · ")),
            )
          : null,
        h("p", { class: "note" }, d.limits, " ", d.attribution, "."),
      ),
    );
  }
}
