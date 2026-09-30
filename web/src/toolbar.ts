// Slim vertical icon bar on the left edge. Each button toggles a window; the alert icon carries a badge.
import { h } from "./dom";
import { iconButton } from "./icons";
import type { IconName } from "./icons";
import { isOpen, onWmChange, resetLayout } from "./wm";
import { T } from "./labels";

export interface Tool {
  /** Window id (also used for pressed state). */
  id: string;
  icon: IconName;
  label: string;
  /** Keyboard shortcut, shown in the tooltip. */
  key?: string;
  toggle: () => void;
}

const buttons = new Map<string, HTMLButtonElement>();
let badge: HTMLElement | null = null;

export function mountToolbar(root: HTMLElement, tools: Tool[]): void {
  root.replaceChildren();
  for (const t of tools) {
    const b = iconButton(t.icon, t.key ? `${t.label} (${t.key})` : t.label, t.toggle, { pressed: false, cls: "tb-btn" });
    buttons.set(t.id, b);
    if (t.id === "queue") {
      badge = h("span", { class: "tb-badge", hidden: true, "aria-live": "polite" });
      b.append(badge);
    }
    root.append(b);
  }
  root.append(h("span", { class: "tb-spacer" }), iconButton("reset", T.resetLayout, resetLayout, { cls: "tb-btn" }));
  const sync = (): void => buttons.forEach((b, id) => b.setAttribute("aria-pressed", String(isOpen(id))));
  onWmChange(sync);
  sync();
}

/** Alerts badge: count of unacknowledged alerts, coloured by the worst priority (info/low/medium/high/critical). */
export function setAlertBadge(count: number, worst: string): void {
  if (!badge) return;
  badge.hidden = count === 0;
  badge.textContent = count > 99 ? "99+" : String(count);
  badge.className = `tb-badge sev-${worst}`;
  buttons.get("queue")?.setAttribute("aria-label", `${T.alerts}: ${count} unacknowledged`);
}
