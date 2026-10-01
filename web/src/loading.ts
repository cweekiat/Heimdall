// Loading screen at start-up (map, areas, alerts) and a small busy pill for slower actions later
// (saving or deleting an area, switching the base map). Never blocks longer than MAX_MS.
import { h } from "./dom";
import { icon } from "./icons";

const MAX_MS = 15_000;

export class Loading {
  private screen: HTMLElement;
  private steps = new Map<string, { label: string; done: boolean }>();
  private list = h("ul", { class: "load-steps" });
  private pill = h("div", { class: "busy-pill", role: "status", "aria-live": "polite", hidden: true });
  private busyN = 0;

  constructor() {
    this.screen = h(
      "div",
      { id: "loading", role: "status", "aria-live": "polite", "aria-label": "Loading Heimdall" },
      h("div", { class: "load-card" }, h("div", { class: "load-brand" }, "HEIMDALL"), h("div", { class: "load-spin" }, icon("spinner", 28)), this.list),
    );
    document.body.append(this.screen, this.pill);
    window.setTimeout(() => this.finish(), MAX_MS);
  }

  /** Register a start-up step; returns the function that marks it done. */
  step(id: string, label: string): () => void {
    this.steps.set(id, { label, done: false });
    this.draw();
    return () => {
      const s = this.steps.get(id);
      if (s && !s.done) {
        s.done = true;
        this.draw();
      }
      if ([...this.steps.values()].every((x) => x.done)) window.setTimeout(() => this.finish(), 250);
    };
  }

  private draw(): void {
    this.list.replaceChildren(...[...this.steps.values()].map((s) => h("li", { class: s.done ? "done" : "" }, s.done ? "✓ " : "… ", s.label)));
  }

  finish(): void {
    if (this.screen.classList.contains("gone")) return;
    this.screen.classList.add("gone");
    window.setTimeout(() => this.screen.remove(), 400);
  }

  /** Show "label…" in a small pill while `p` runs (overlapping calls share the pill). */
  async busy<T>(label: string, p: Promise<T>): Promise<T> {
    this.busyN += 1;
    this.pill.hidden = false;
    this.pill.replaceChildren(icon("spinner", 14), h("span", {}, `${label}…`));
    try {
      return await p;
    } finally {
      this.busyN -= 1;
      if (this.busyN === 0) this.pill.hidden = true;
    }
  }
}
