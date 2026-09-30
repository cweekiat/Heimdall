// Data sources window: monitoring and control. Refreshes every 10 s while open.
// Never shows key values: needs-key rows only show the server's plain hint ("Add X_KEY to .env").
import { api } from "./api";
import type { SourceState } from "./api";
import { ago, h } from "./dom";
import { icon, iconButton } from "./icons";
import { KeysSection } from "./keysui";
import type { IconName } from "./icons";
import { T } from "./labels";
import { table } from "./panels";
import { createWindow, EDGE_TOP, isOpen } from "./wm";

const DOMAIN_ICON: Record<string, IconName> = { air: "plane", sea: "ship", space: "radio", cyber: "waypoints", events: "notice" };
const STATUS: Record<SourceState["status"], { dot: string; word: string }> = {
  ok: { dot: "ok", word: "OK" },
  degraded: { dot: "warn", word: "Slow" },
  down: { dot: "bad", word: "Down" },
  needs_key: { dot: "key", word: "Needs key" },
  disabled: { dot: "off", word: "Off" },
  off: { dot: "off", word: "Off" },
};

export class Sources {
  list: SourceState[] = [];
  onChange: () => void = () => undefined;
  private body: HTMLElement;
  private confirm: string | null = null;
  private error = "";
  private timer = 0;
  private busy = false;
  private keys = new KeysSection({ list: () => api.keys(), put: (n, v) => api.putKey(n, v), del: (n) => api.deleteKey(n) });

  constructor() {
    this.body = createWindow({
      id: "sources",
      title: T.sources,
      icon: "sources",
      x: -60,
      y: EDGE_TOP + 8,
      w: 640,
      h: 420,
      onOpen: () => {
        void this.refresh();
        void this.keys.load();
        this.timer = window.setInterval(() => {
          if (document.visibilityState !== "hidden") void this.refresh();
        }, 10_000);
      },
      onClose: () => window.clearInterval(this.timer),
    });
    this.wireVisibility();
  }

  private wireVisibility(): void {
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
    this.confirm = null;
    try {
      await api.setSource(s.key, on);
      this.error = "";
    } catch (e) {
      this.error = String(e);
    }
    await this.refresh();
  }

  render(): void {
    const rows = this.list.map((s) => {
      const st = STATUS[s.status];
      const sw = h("input", { type: "checkbox", role: "switch", "aria-label": `${s.label}: ${s.enabled ? "on" : "off"}` });
      sw.checked = s.enabled;
      sw.addEventListener("change", () => {
        if (!sw.checked) {
          sw.checked = true; // stays on until the analyst confirms
          this.confirm = s.key;
          this.render();
        } else void this.toggle(s, true);
      });
      const name = h(
        "div",
        {},
        h("strong", {}, s.label),
        s.detail ? h("div", { class: "note" }, s.detail) : null,
        this.confirm === s.key
          ? h(
              "div",
              { class: "confirm", role: "alert" },
              h("span", {}, T.stopCollecting(s.label)),
              h("button", { class: "danger", type: "button", onclick: () => void this.toggle(s, false) }, T.stop),
              h("button", { type: "button", onclick: () => { this.confirm = null; this.render(); } }, T.keep),
            )
          : null,
      );
      return [
        h("span", { title: s.domain }, icon(DOMAIN_ICON[s.domain] ?? "radio", 16)),
        name,
        h("span", { class: "stat" }, h("span", { class: `dot ${st.dot}`, "aria-hidden": "true" }), st.dot === "key" ? icon("dashed", 14) : null, st.word),
        s.enabled && s.last_ok ? ago(s.last_ok) : "-",
        h("span", { class: "num" }, s.enabled ? String(s.records_last_hour) : "-"),
        h("label", { class: "sw" }, sw, h("span", {}, s.enabled ? T.on : T.off)),
      ];
    });
    this.body.replaceChildren(
      h(
        "div",
        { class: "body" },
        h("div", { class: "row" }, h("p", { class: "note" }, T.sourcesNote), iconButton("reset", T.refresh, () => void this.refresh())),
        this.error ? h("div", { class: "fail", role: "alert" }, this.error) : null,
        this.list.length === 0 && !this.error ? h("div", { class: "empty" }, T.placeholderSources) : table(["", "Source", "Status", "Last update", "Per hour", "Collect"], rows),
        this.keys.el,
      ),
    );
  }
}
