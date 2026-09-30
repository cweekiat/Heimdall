// About window: renders two bundled markdown files as tabs. The renderer builds DOM nodes with
// textContent only (no innerHTML), so the output is sanitised by construction; links are http(s) only.
import { h } from "./dom";
import { createWindow, EDGE_LEFT, EDGE_TOP, getBody, isOpen, openWindow } from "./wm";
import why from "./about/why-it-matters.md?raw";
import building from "./about/building-heimdall.md?raw";

const PAGES: [string, string, string][] = [
  ["why", "Why it matters", why],
  ["build", "Building Heimdall", building],
];

/** Inline: **bold**, *italic*, `code`, [text](https://url). Everything else is plain text. */
function inline(src: string): Node[] {
  const out: Node[] = [];
  const re = /\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let last = 0;
  for (let m = re.exec(src); m; m = re.exec(src)) {
    if (m.index > last) out.push(document.createTextNode(src.slice(last, m.index)));
    if (m[1] !== undefined) out.push(h("strong", {}, m[1]));
    else if (m[2] !== undefined) out.push(h("em", {}, m[2]));
    else if (m[3] !== undefined) out.push(h("code", {}, m[3]));
    else out.push(h("a", { href: m[5]!, target: "_blank", rel: "noopener noreferrer" }, m[4]!));
    last = m.index + m[0].length;
  }
  if (last < src.length) out.push(document.createTextNode(src.slice(last)));
  return out;
}

const cells = (line: string): string[] => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

export function renderMarkdown(md: string): HTMLElement {
  const root = h("div", { class: "md" });
  const lines = md.replace(/\r/g, "").split("\n");
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i]!;
    const head = /^(#{1,4})\s+(.*)$/.exec(ln);
    if (!ln.trim()) i++;
    else if (head) {
      root.append(h((["h2", "h3", "h4", "h5"] as const)[head[1]!.length - 1]!, {},...inline(head[2]!)));
      i++;
    } else if (/^\s*\|/.test(ln)) {
      const rows: string[][] = [];
      while (i < lines.length && /^\s*\|/.test(lines[i]!)) {
        if (!/^\s*\|[\s:|-]+\|?\s*$/.test(lines[i]!)) rows.push(cells(lines[i]!));
        i++;
      }
      const [hd, ...body] = rows;
      root.append(
        h(
          "div",
          { class: "md-table" },
          h(
            "table",
            {},
            h("thead", {}, h("tr", {}, ...(hd ?? []).map((c) => h("th", {}, ...inline(c))))),
            h("tbody", {}, ...body.map((r) => h("tr", {}, ...r.map((c) => h("td", {}, ...inline(c)))))),
          ),
        ),
      );
    } else if (/^\s*([-*]|\d+\.)\s+/.test(ln)) {
      const ordered = /^\s*\d+\./.test(ln);
      const items: Node[] = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i]!)) {
        items.push(h("li", {}, ...inline(lines[i]!.replace(/^\s*([-*]|\d+\.)\s+/, ""))));
        i++;
      }
      root.append(h(ordered ? "ol" : "ul", {}, ...items));
    } else if (/^>\s?/.test(ln)) {
      const buf: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i]!)) buf.push(lines[i++]!.replace(/^>\s?/, ""));
      root.append(h("blockquote", {}, ...inline(buf.join(" "))));
    } else {
      const buf: string[] = [];
      while (i < lines.length && lines[i]!.trim() && !/^(#{1,4}\s|\s*\||\s*([-*]|\d+\.)\s|>)/.test(lines[i]!)) buf.push(lines[i++]!.trim());
      root.append(h("p", {}, ...inline(buf.join(" "))));
    }
  }
  return root;
}

export function showAbout(): void {
  if (!isOpen("about")) {
    createWindow({ id: "about", title: "About", icon: "help", w: 620, h: 620, x: EDGE_LEFT + 60, y: EDGE_TOP + 20, minW: 320 });
    const body = getBody("about")!;
    const panels = PAGES.map(([, , md]) => h("div", { class: "about-page", role: "tabpanel" }, renderMarkdown(md)));
    const tabs = PAGES.map(([id, label], n) =>
      h("button", { type: "button", role: "tab", id: `about-tab-${id}`, class: "about-tab", onclick: () => pick(n) }, label),
    );
    const pick = (n: number): void => {
      tabs.forEach((t, k) => {
        t.setAttribute("aria-selected", String(k === n));
        t.tabIndex = k === n ? 0 : -1;
      });
      panels.forEach((p, k) => (p.hidden = k !== n));
    };
    body.replaceChildren(h("div", { class: "about-tabs", role: "tablist", "aria-label": "About Heimdall" }, ...tabs), ...panels);
    pick(0);
  }
  openWindow("about");
}
