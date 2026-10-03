// World news headlines (task 049, rail "News", key n). Context for the watch officer only: headlines are untrusted
// text written via textContent; Heimdall never turns them into alerts and the shift brief never reads them.
import { api } from "./api";
import type { NewsItem } from "./api";
import { h } from "./dom";
import { fmtDT } from "./tz";
import { createWindow, EDGE_LEFT, EDGE_TOP, isOpen } from "./wm";

export class NewsWindow {
  readonly body: HTMLElement;
  private items: NewsItem[] = [];
  private error = "";
  private q = "";

  constructor() {
    this.body = createWindow({ id: "news", title: "World news", icon: "notice", x: EDGE_LEFT + 8, y: EDGE_TOP + 8, w: 420, h: 560, minW: 300 });
    this.render();
  }

  start(): void {
    void this.refresh();
    window.setInterval(() => isOpen("news") && void this.refresh(), 300_000);
  }

  async refresh(): Promise<void> {
    try {
      this.items = (await api.news(60)).items;
      this.error = "";
    } catch (e) {
      this.error = `News unavailable (${String(e).slice(0, 60)})`;
    }
    this.render();
  }

  private render(): void {
    const q = this.q.toLowerCase();
    const list = this.items.filter((i) => !q || i.title.toLowerCase().includes(q));
    const search = h("input", { type: "search", placeholder: "Filter headlines (e.g. Taiwan, drone)", "aria-label": "Filter headlines", value: this.q });
    search.addEventListener("input", () => {
      this.q = search.value;
      const pos = search.selectionStart;
      this.render();
      const el = this.body.querySelector<HTMLInputElement>("input[type=search]");
      el?.focus();
      if (pos !== null) el?.setSelectionRange(pos, pos);
    });
    this.body.replaceChildren(
      h(
        "div",
        { class: "body news" },
        h("p", { class: "note" }, "Headlines for context only (BBC World, Al Jazeera). Heimdall never reads them into alerts or the AI brief."),
        search,
        this.error ? h("p", { class: "note warn" }, this.error) : null,
        h(
          "ul",
          { class: "news-list" },
          ...(list.length
            ? list.map((i) =>
                h(
                  "li",
                  {},
                  i.url ? h("a", { href: i.url, target: "_blank", rel: "noopener noreferrer" }, i.title) : h("span", {}, i.title),
                  h("small", {}, [i.feed, fmtDT(i.ts)].filter(Boolean).join(" · ")),
                ),
              )
            : [h("li", { class: "empty" }, this.items.length ? "No headline matches." : "No headlines yet (Data sources → World news).")]),
        ),
      ),
    );
  }
}
