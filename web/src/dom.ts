// Tiny DOM helper. Text is ALWAYS set via textContent: scraped/news strings are untrusted data.

type Child = Node | string | number | null | undefined | false;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | boolean | ((e: Event) => void)> = {},
  ...kids: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (typeof v === "function") el.addEventListener(k.replace(/^on/, ""), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false) el.setAttribute(k, v);
  }
  for (const c of kids) {
    if (c === null || c === undefined || c === false) continue;
    el.append(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const fmtTime = (iso: string | null): string =>
  iso ? new Date(iso).toISOString().slice(5, 16).replace("T", " ") + "Z" : "-";

export const fmtNum = (v: number | null | undefined, d = 2): string =>
  v === null || v === undefined ? "-" : Number.isInteger(v) ? String(v) : v.toFixed(d);

export function ago(iso: string | null): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}
