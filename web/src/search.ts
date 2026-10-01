// Top-centre search (Google Earth style): one box for coordinates, areas of operation, alerts and
// the aircraft / ships loaded on the map. Results are local data only: no geocoding service is called.
import { h } from "./dom";
import { icon } from "./icons";

export interface Hit {
  group: string;
  label: string;
  detail?: string;
  pick: () => void;
}

/** "1.29, 103.85" / "1.29 103.85" / "1.29N 103.85E" -> [lat, lon]; null when it is not a coordinate. */
export function parseLatLon(q: string): [number, number] | null {
  const m = q.trim().match(/^(-?\d+(?:\.\d+)?)\s*°?\s*([NS])?[\s,;]+(-?\d+(?:\.\d+)?)\s*°?\s*([EW])?$/i);
  if (!m) return null;
  let lat = Number(m[1]);
  let lon = Number(m[3]);
  if (m[2]?.toUpperCase() === "S") lat = -Math.abs(lat);
  if (m[4]?.toUpperCase() === "W") lon = -Math.abs(lon);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
  return [lat, lon];
}

export interface SearchCtx {
  /** All hits for a query (already grouped and ordered). */
  find: (q: string) => Hit[];
}

export function mountSearch(root: HTMLElement, ctx: SearchCtx): { focus: () => void } {
  const input = h("input", {
    type: "search",
    id: "global-q",
    placeholder: "Search areas, alerts, flights, ships or lat, lon",
    "aria-label": "Search",
    autocomplete: "off",
    role: "combobox",
    "aria-expanded": "false",
    "aria-controls": "search-results",
  });
  const list = h("ul", { id: "search-results", class: "search-results", role: "listbox", hidden: true });
  let hits: Hit[] = [];
  let cur = -1;
  const close = (): void => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    cur = -1;
  };
  const choose = (i: number): void => {
    const x = hits[i];
    if (!x) return;
    close();
    input.blur();
    x.pick();
  };
  const draw = (): void => {
    const q = input.value.trim();
    hits = q ? ctx.find(q) : [];
    if (!q) return close();
    let last = "";
    list.replaceChildren(
      ...(hits.length
        ? hits.flatMap((x, i) => {
            const head = x.group !== last ? [h("li", { class: "search-group", role: "presentation" }, x.group)] : [];
            last = x.group;
            return [
              ...head,
              h(
                "li",
                { role: "option", class: "search-hit", id: `sh-${i}`, "aria-selected": String(i === cur), onmousedown: (e: Event) => (e.preventDefault(), choose(i)) },
                h("span", { class: "search-label" }, x.label),
                x.detail ? h("span", { class: "note" }, x.detail) : null,
              ),
            ];
          })
        : [h("li", { class: "search-empty note" }, "Nothing found in areas, alerts or loaded traffic. Try a callsign, MMSI, alert reference or lat, lon.")]),
    );
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };
  input.addEventListener("input", () => {
    cur = -1;
    draw();
  });
  input.addEventListener("focus", () => input.value.trim() && draw());
  input.addEventListener("blur", () => window.setTimeout(close, 120));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!hits.length) return;
      cur = (cur + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length;
      draw();
      input.setAttribute("aria-activedescendant", `sh-${cur}`);
    } else if (e.key === "Enter") {
      choose(cur >= 0 ? cur : 0);
    } else if (e.key === "Escape") {
      input.value = "";
      close();
    }
  });
  root.replaceChildren(h("div", { class: "search-box" }, icon("search", 18), input), list);
  return { focus: () => input.focus() };
}
