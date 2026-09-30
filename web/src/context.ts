// Alert context (task 023c): what could explain an alert, and when it can be checked independently.
// Client for GET /api/alerts/{id}/context plus a DOM renderer. Every value is set via textContent
// (the `h` helper); the catalogue link is plain text and is never opened or fetched.
// Imports carry ".ts" so the offline tests can run this file directly under node.
import { h } from "./dom.ts";

export interface Confounder {
  note: string;
  factor: number | null;
}
export interface Observability {
  note: string;
  platform: string | null;
  sensor: string | null;
  time: string | null;
  kind: "footprint" | "overpass";
  id?: string;
  cloud_cover?: number | null;
  url?: string | null;
}
export interface AlertContext {
  confounders: Confounder[];
  observability: Observability[];
  gaps: string[];
}

export const EMPTY_WEATHER = "No weather data yet (archive lag)";
export const EMPTY_IMAGERY = "No imagery in the last 48 h";
export const EMPTY_PASSES = "No satellite passes listed for the next 48 h";
export const NOT_CHECKED = "Not checked yet";

const cache = new Map<string, AlertContext>();

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
const s = (v: unknown): string | null => (typeof v === "string" ? v : null);

/** Tolerant parse: unknown shapes become an empty context, never an exception. */
export function parseContext(raw: unknown): AlertContext {
  const r = isObj(raw) ? raw : {};
  const list = (k: string): unknown[] => (Array.isArray(r[k]) ? (r[k] as unknown[]) : []);
  return {
    confounders: list("confounders").filter(isObj).map((c) => ({ note: s(c.note) ?? "", factor: typeof c.factor === "number" ? c.factor : null })),
    observability: list("observability")
      .filter(isObj)
      .map((o) => ({
        note: s(o.note) ?? "",
        platform: s(o.platform),
        sensor: s(o.sensor),
        time: s(o.time),
        kind: o.kind === "overpass" ? ("overpass" as const) : ("footprint" as const),
        id: s(o.id) ?? undefined,
        cloud_cover: typeof o.cloud_cover === "number" ? o.cloud_cover : null,
        url: s(o.url),
      })),
    gaps: list("gaps").filter((g): g is string => typeof g === "string"),
  };
}

export async function getAlertContext(id: string, force = false): Promise<AlertContext> {
  const hit = cache.get(id);
  if (hit && !force) return hit;
  const r = await fetch(`/api/alerts/${encodeURIComponent(id)}/context`);
  if (!r.ok) throw new Error(`${r.status}`);
  const ctx = parseContext(await r.json());
  cache.set(id, ctx);
  return ctx;
}

const utc = (iso: string | null): string => (iso && !Number.isNaN(Date.parse(iso)) ? `${new Date(iso).toISOString().slice(0, 16).replace("T", " ")} UTC` : "time unknown");
const local = (iso: string | null, tz?: string): string =>
  iso && !Number.isNaN(Date.parse(iso))
    ? new Date(iso).toLocaleString("en-GB", { timeZone: tz, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false })
    : "";

/** Plain-words effect on confidence, e.g. "confidence lowered x0.8". */
export function effect(c: Confounder): string {
  if (c.factor === null) return "confidence lowered";
  return `confidence lowered ×${c.factor}`;
}

/** Spell out shorthand: a bare "Kp 6" becomes "geomagnetic storm (Kp 6)". */
export function gloss(note: string): string {
  return /Kp\s?\d/.test(note) && !/geomagnetic/i.test(note) ? note.replace(/Kp\s?(\d)/, "geomagnetic storm (Kp $1)") : note;
}

export interface CtxOpts {
  /** Show one imagery footprint on the map (turns the layer on when needed). */
  onFootprint?: (featureId: string) => void;
  tz?: string;
}

/** Hook set by main.ts: select a footprint on the map (and switch the imagery layer on). */
export const ctxHooks: { onFootprint?: (featureId: string) => void } = {};

/** Section for the inspector: shows a placeholder, then fills in when the context arrives. */
export function ctxSection(alertId: string): HTMLElement {
  const box = h("div", { class: "ctx-box" });
  const show = (c: AlertContext | null): void => box.replaceChildren(renderContext(c, { onFootprint: (id) => ctxHooks.onFootprint?.(id) }));
  show(cache.get(alertId) ?? null);
  if (!cache.has(alertId)) void getAlertContext(alertId).then(show, () => undefined);
  return box;
}

export function renderContext(ctx: AlertContext | null, opts: CtxOpts = {}): HTMLElement {
  if (!ctx) return h("div", { class: "ctx note" }, `Context ${NOT_CHECKED.toLowerCase()}.`);
  const feet = ctx.observability.filter((o) => o.kind === "footprint");
  const pass = ctx.observability.filter((o) => o.kind === "overpass").slice(0, 3);
  return h(
    "div",
    { class: "ctx" },
    h("h3", {}, "Things that could explain this"),
    ctx.confounders.length
      ? h("ul", {}, ...ctx.confounders.map((c) => h("li", {}, `${gloss(c.note)} `, h("span", { class: "note" }, `(${effect(c)})`))))
      : h("div", { class: "note" }, EMPTY_WEATHER),
    ctx.gaps.length ? h("ul", { class: "note" }, ...ctx.gaps.map((g) => h("li", {}, g))) : null,
    h("h3", {}, "Can this be checked?"),
    h("div", { class: "note" }, "Recent satellite images"),
    feet.length
      ? h(
          "ul",
          {},
          ...feet.map((o) => {
            const cc = o.cloud_cover === null || o.cloud_cover === undefined ? "radar, sees through cloud" : `cloud ${Math.round(o.cloud_cover)}%`;
            return h(
              "li",
              {},
              `${o.platform ?? "satellite"} · ${utc(o.time)} · ${cc} `,
              o.id && opts.onFootprint ? h("button", { type: "button", class: "ctx-link", onclick: () => opts.onFootprint?.(o.id!) }, "Show on map") : null,
              o.url ? h("div", { class: "note" }, o.url) : null,
            );
          }),
        )
      : h("div", { class: "note" }, EMPTY_IMAGERY),
    h("div", { class: "note" }, "Next passes over this area"),
    pass.length
      ? h("ul", {}, ...pass.map((o) => h("li", {}, `${o.platform ?? "satellite"}${o.sensor ? ` (${o.sensor})` : ""} · ${utc(o.time)} · ${local(o.time, opts.tz)} local`)))
      : h("div", { class: "note" }, `${EMPTY_PASSES}. ${NOT_CHECKED}.`),
  );
}
