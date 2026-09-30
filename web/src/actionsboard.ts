// Actions board: every proposed action across alerts. Approve and reject only on proposed cards;
// high-risk items need a second click. Actions come only from the server's allow-listed playbook.
import { api } from "./api";
import type { BoardAction } from "./api";
import { ago, h } from "./dom";
import { kindLabel, T } from "./labels";
import { createWindow, EDGE_TOP, isOpen } from "./wm";
import { selectAlert } from "./store";

const COLUMNS: [string, string, string[]][] = [
  ["proposed", T.colProposed, ["proposed"]],
  ["approved", T.colApproved, ["approved"]],
  ["done", T.colDone, ["done", "executed", "completed"]],
  ["rejected", T.colRejected, ["rejected"]],
];

export class ActionsBoard {
  private body: HTMLElement;
  private items: BoardAction[] = [];
  private confirm: string | null = null;
  private error = "";
  private timer = 0;

  constructor() {
    this.body = createWindow({
      id: "actions",
      title: T.actions,
      icon: "actions",
      x: -60,
      y: EDGE_TOP + 8,
      w: 720,
      h: 440,
      minW: 320,
      onOpen: () => {
        void this.refresh();
        this.timer = window.setInterval(() => void this.refresh(), 15_000);
      },
      onClose: () => window.clearInterval(this.timer),
    });
    this.body.classList.add("actions-win");
  }

  /** Open proposals, for a badge or shift report. */
  proposed(): number {
    return this.items.filter((i) => i.status === "proposed").length;
  }

  async refresh(): Promise<void> {
    try {
      this.items = await api.board();
      this.error = "";
    } catch (e) {
      this.error = String(e);
    }
    if (isOpen("actions")) this.render();
  }

  private async decide(a: BoardAction, approve: boolean): Promise<void> {
    this.confirm = null;
    try {
      await (approve ? api.approve(a.id) : api.reject(a.id));
      this.error = "";
    } catch (e) {
      this.error = String(e);
    }
    await this.refresh();
  }

  private card(a: BoardAction): HTMLElement {
    const proposed = a.status === "proposed";
    const asking = this.confirm === a.id;
    return h(
      "div",
      { class: "acard" },
      h("div", {}, h("strong", {}, a.label), a.risk === "high" ? h("span", { class: "badge risk" }, T.highRisk) : null),
      h("button", { class: "link", type: "button", onclick: () => void selectAlert(a.alert.id) }, kindLabel(a.alert.kind)),
      h("div", { class: "note" }, ago(a.ts)),
      proposed && !asking
        ? h(
            "div",
            { class: "row" },
            h("button", { class: "primary", type: "button", onclick: () => (a.risk === "high" ? ((this.confirm = a.id), this.render()) : void this.decide(a, true)) }, T.approve),
            h("button", { type: "button", onclick: () => void this.decide(a, false) }, T.reject),
          )
        : null,
      proposed && asking
        ? h(
            "div",
            { class: "confirm", role: "alert" },
            h("span", {}, T.confirmHighRisk(a.label)),
            h("button", { class: "danger", type: "button", onclick: () => void this.decide(a, true) }, T.confirmApprove),
            h("button", { type: "button", onclick: () => { this.confirm = null; this.render(); } }, T.cancel),
          )
        : null,
    );
  }

  render(): void {
    this.body.replaceChildren(
      ...(this.error ? [h("div", { class: "fail", role: "alert" }, this.error)] : []),
      h("p", { class: "note" }, T.actionsNote),
      h(
        "div",
        { class: "kanban" },
        ...COLUMNS.map(([, label, statuses]) => {
          const cards = this.items.filter((i) => statuses.includes(i.status));
          return h("section", { class: "kcol", "aria-label": label }, h("h3", {}, `${label} (${cards.length})`), ...(cards.length ? cards.map((c) => this.card(c)) : [h("div", { class: "note" }, T.noneToShow)]));
        }),
      ),
    );
  }
}
