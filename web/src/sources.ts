// Data sources page: what is collecting, grouped by kind, with plain names. The switch turns
// collection on or off at once (no confirmation). Clicking a source that needs a key opens a small
// pop-up to paste it (write-only: the value is never shown again). Refreshes every 10 s while open.
import { api } from "./api";
import type { SourceState } from "./api";
import { ago, h } from "./dom";
import { icon } from "./icons";
import { KeysSection } from "./keysui";
import type { IconName } from "./icons";
import { T } from "./labels";
import { createWindow, EDGE_LEFT, EDGE_TOP, isOpen } from "./wm";

const GROUPS: { title: string; icon: IconName; domains: string[] }[] = [
  { title: "Aircraft and notices", icon: "plane", domains: ["air"] },
  { title: "Ships and maritime warnings", icon: "ship", domains: ["sea"] },
  { title: "Radio spectrum", icon: "radio", domains: ["spectrum"] },
  { title: "Satellites, space weather and weather", icon: "orbit", domains: ["space", "events", "cyber"] },
];
const STATUS: Record<SourceState["status"], { dot: string; word: string }> = {
  ok: { dot: "ok", word: "Working" },
  degraded: { dot: "warn", word: "Problems" },
  down: { dot: "bad", word: "Stopped" },
  needs_key: { dot: "key", word: "Needs a key" },
  disabled: { dot: "off", word: "Not available" },
  off: { dot: "off", word: "Off" },
};

export class Sources {
  list: SourceState[] = [];
  onChange: () => void = () => undefined;
  private body: HTMLElement;
  private error = "";
  private timer = 0;
  private busy = false;
  private pending = new Set<string>();
  private keys = new KeysSection({ list: () => api.keys(), put: (n, v) => api.putKey(n, v), del: (n) => api.deleteKey(n) });
  private modal = h("div", { class: "modal-back", hidden: true });

  constructor() {
    this.body = createWindow({
      id: "sources",
      title: T.sources,
      icon: "sources",
      x: EDGE_LEFT + 8,
      y: EDGE_TOP + 8,
      w: 520,
      h: 620,
      onOpen: () => {
        void this.refresh();
        this.timer = window.setInterval(() => {
          if (document.visibilityState !== "hidden") void this.refresh();
        }, 10_000);
      },
      onClose: () => {
        window.clearInterval(this.timer);
        this.closeKeys();
      },
    });
    document.body.append(this.modal);
    this.modal.addEventListener("click", (e) => e.target === this.modal && this.closeKeys());
    document.addEventListener("keydown", (e) => e.key === "Escape" && !this.modal.hidden && this.closeKeys());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && isOpen("sources")) void this.refresh();
    });
  }

  async refresh(): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      this.list = await api.sources();
      this.error = "";
    } catch (e) {
      this.error = String(e);
    } finally {
      this.busy = false;
    }
    this.onChange();
    if (isOpen("sources")) this.render();
  }

  private async toggle(s: SourceState, on: boolean): Promise<void> {
    this.pending.add(s.key);
    s.enabled = on; // show the new state straight away
    this.render();
    try {
      await api.setSource(s.key, on);
      this.error = "";
    } catch (e) {
      this.error = String(e);
    }
    this.pending.delete(s.key);
    await this.refresh();
  }

  private openKeys(s: SourceState): void {
    this.keys.setOnly(s.keys);
    void this.keys.load();
    this.modal.hidden = false;
    this.modal.replaceChildren(
      h(
        "div",
        { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": `API key for ${s.label}` },
        h("div", { class: "modal-head" }, h("strong", {}, s.label), h("button", { type: "button", class: "win-btn", "aria-label": "Close", onclick: () => this.closeKeys() }, icon("close"))),
        s.about ? h("p", { class: "note" }, s.about) : null,
        this.keys.el,
        h("p", { class: "note" }, "The source restarts by itself after a key is saved."),
      ),
    );
    window.setTimeout(() => this.modal.querySelector<HTMLElement>("input,button")?.focus(), 50);
  }

  private closeKeys(): void {
    if (this.modal.hidden) return;
    this.modal.hidden = true;
    void this.refresh();
  }

  private row(s: SourceState): HTMLElement {
    const st = STATUS[s.status];
    const sw = h("input", { type: "checkbox", role: "switch", "aria-label": `Collect from ${s.label}` });
    sw.checked = s.enabled;
    sw.disabled = this.pending.has(s.key);
    sw.addEventListener("click", (e) => e.stopPropagation());
    sw.addEventListener("change", () => void this.toggle(s, sw.checked));
    const keyBtn = s.keys.length
      ? h("button", { type: "button", class: `key-btn${s.status === "needs_key" ? " primary" : ""}`, onclick: (e: Event) => (e.stopPropagation(), this.openKeys(s)) }, icon("key", 14), s.status === "needs_key" ? "Add key" : "Key")
      : null;
    return h(
      "li",
      {
        class: `src-row${s.keys.length ? " has-key" : ""}`,
        title: s.keys.length ? "Click to add or change its API key" : s.about ?? s.label,
        onclick: () => s.keys.length && this.openKeys(s),
      },
      h("div", { class: "src-main" }, h("strong", {}, s.label), s.about ? h("span", { class: "note" }, s.about) : null, s.detail && !["ok", "needs_key"].includes(s.status) ? h("span", { class: "note src-detail" }, s.detail) : null),
      h("div", { class: "src-stat" }, h("span", { class: "stat" }, h("span", { class: `dot ${st.dot}`, "aria-hidden": "true" }), st.word), h("span", { class: "note" }, s.enabled && s.last_ok ? `updated ${ago(s.last_ok)}` : "")),
      keyBtn,
      h("label", { class: "sw", title: s.enabled ? "Collecting: click to stop" : "Off: click to start collecting" }, sw, h("span", {}, s.enabled ? T.on : T.off)),
    );
  }

  render(): void {
    const groups = GROUPS.map((g) => ({ g, rows: this.list.filter((s) => g.domains.includes(s.domain)) })).filter((x) => x.rows.length);
    this.body.replaceChildren(
      h(
        "div",
        { class: "body sources-page" },
        h("p", { class: "note" }, T.sourcesNote),
        this.error ? h("div", { class: "fail", role: "alert" }, this.error) : null,
        this.list.length === 0 && !this.error ? h("div", { class: "empty" }, T.placeholderSources) : null,
        ...groups.map(({ g, rows }) => h("section", { class: "src-group" }, h("h3", {}, icon(g.icon, 15), g.title), h("ul", { class: "src-list2" }, ...rows.map((s) => this.row(s))))),
      ),
    );
  }
}
