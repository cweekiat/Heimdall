// Context-layer helpers (task 049). Pure: no DOM, no deck.gl.
import { fmtDT } from "./tz.ts";

/** Radius in px for a conflict event by number of reporting sources. */
export const conflictRadius = (sources: number): number => Math.min(14, 4 + 2.5 * Math.log2(Math.max(1, sources)));

export function conflictText(p: Record<string, unknown>, tz: (iso: string) => string = fmtDT): string {
  const n = typeof p.sources === "number" ? p.sources : 0;
  return [
    String(p.label ?? "Conflict event"),
    p.place ? String(p.place) : null,
    `${n} source${n === 1 ? "" : "s"}${p.domain ? ` (e.g. ${String(p.domain)})` : ""}`,
    typeof p.ts === "string" ? tz(p.ts) : null,
    "GDELT machine-coded news: may be wrong or old",
  ]
    .filter(Boolean)
    .join(" · ");
}
