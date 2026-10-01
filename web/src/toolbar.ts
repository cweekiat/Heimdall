// Left rail (Google Earth style): icons only, widening on hover or keyboard focus to show each label.
// Each entry toggles a window; pressed = that window is open.
import { h } from "./dom";
import { icon } from "./icons";
import type { IconName } from "./icons";
import { isOpen, onWmChange } from "./wm";

export interface Tool {
  /** Window id (also used for pressed state). */
  id: string;
  icon: IconName;
  label: string;
  /** Keyboard shortcut, shown next to the label. */
  key?: string;
  toggle: () => void;
  /** Short description shown under the label when the rail is expanded. */
  hint?: string;
}

const buttons = new Map<string, HTMLButtonElement>();

export function mountToolbar(root: HTMLElement, tools: Tool[], bottom: Tool[] = []): void {
  root.replaceChildren();
  const make = (t: Tool): HTMLButtonElement => {
    const b = h(
      "button",
      { type: "button", class: "rail-btn", "aria-pressed": "false", "aria-label": t.label, title: t.key ? `${t.label} (${t.key})` : t.label, onclick: t.toggle },
      icon(t.icon, 20),
      h("span", { class: "rail-text" }, h("span", { class: "rail-label" }, t.label, t.key ? h("kbd", {}, t.key) : null), t.hint ? h("span", { class: "rail-hint" }, t.hint) : null),
    ) as HTMLButtonElement;
    buttons.set(t.id, b);
    return b;
  };
  root.append(...tools.map(make), h("span", { class: "rail-spacer" }), ...bottom.map(make));
  const sync = (): void => buttons.forEach((b, id) => b.setAttribute("aria-pressed", String(isOpen(id))));
  onWmChange(sync);
  sync();
}
