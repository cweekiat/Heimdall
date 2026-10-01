// Tour overlay: dims the page, outlines the real element and shows a step card. Text via h() only.
import { h } from "../dom";
import { selectAlert, state } from "../store";
import { closeWindow, isOpen, openWindow } from "../wm";
import { markTourSeen, TourController, tourSeen } from "./controller";
import type { TourStep } from "./steps";
import { DEMO_VIEW, tourDemo } from "./demo";

/** Wired by main: move the camera and redraw the map for example scenes. */
export const tourMap: { view: (lon: number, lat: number, zoom: number) => void; redraw: () => void; home: () => void } = {
  view: () => undefined,
  redraw: () => undefined,
  home: () => undefined,
};

let ctl: TourController | null = null;
let root: HTMLElement | null = null;
let ring: HTMLElement | null = null;
let card: HTMLElement | null = null;
let current: TourStep | null = null;
let opened: string[] = [];

const storage = (): Storage | null => {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

const visible = (sel: string): HTMLElement | null => {
  const el = document.querySelector<HTMLElement>(sel);
  if (!el || el.hidden) return null;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 ? el : null;
};

function place(): void {
  if (!ring || !card || !current) return;
  const el = visible(current.anchor);
  if (!el) return;
  const pad = 6;
  const r = el.getBoundingClientRect();
  ring.style.cssText = `left:${r.left - pad}px;top:${r.top - pad}px;width:${r.width + 2 * pad}px;height:${r.height + 2 * pad}px`;
  const narrow = window.innerWidth < 700;
  const cw = Math.min(340, window.innerWidth - 16);
  let left = narrow ? 8 : r.right + 16;
  if (!narrow && left + cw > window.innerWidth - 8) left = Math.max(8, r.left - cw - 16);
  const top = narrow
    ? r.top > window.innerHeight / 2 ? 60 : window.innerHeight - 280
    : Math.min(Math.max(8, r.top), window.innerHeight - 280);
  card.style.cssText = `left:${left}px;top:${top}px;width:${cw}px`;
}

function render(s: TourStep | null, i: number, total: number): void {
  current = s;
  if (!root || !card) return;
  if (!s) {
    root.hidden = true;
    return;
  }
  root.hidden = false;
  card.replaceChildren(
    h("div", { class: "tour-phase" }, `Step ${i + 1} of ${total}${s.demo ? " · example data on the map" : ""}`),
    h("h2", { id: "tour-title" }, s.title),
    h("p", {}, s.body),
    h(
      "div",
      { class: "tour-btns" },
      h("button", { type: "button", class: "tour-skip", onclick: () => ctl?.end() }, "Skip tour"),
      h("span", { class: "tb-spacer" }),
      h("button", { type: "button", onclick: () => ctl?.back(), disabled: i === 0 }, "Back"),
      h("button", { type: "button", class: "tour-next", onclick: () => ctl?.next() }, i === total - 1 ? "Finish" : "Next"),
    ),
  );
  place();
  card.querySelector<HTMLElement>(".tour-next")?.focus();
}

function prepare(s: TourStep): void {
  const was = tourDemo.scene;
  tourDemo.scene = s.demo ?? null;
  if (s.demo) tourMap.view(...DEMO_VIEW[s.demo]);
  if (was !== tourDemo.scene) tourMap.redraw();
  if (s.open && !isOpen(s.open)) {
    openWindow(s.open);
    opened.push(s.open);
  }
  if (s.needsAlert) {
    if (state.selected === null && state.alerts[0]) void selectAlert(state.alerts[0].id, false);
    if (!isOpen("inspector") && state.selected !== null) openWindow("inspector");
  }
}

function ensure(): TourController {
  if (ctl) return ctl;
  ring = h("div", { class: "tour-ring", "aria-hidden": "true" });
  card = h("div", { class: "tour-card", role: "dialog", "aria-modal": "true", "aria-labelledby": "tour-title" });
  root = h("div", { id: "tour", hidden: true }, h("div", { class: "tour-shade" }), ring, card);
  document.body.append(root);
  window.addEventListener("resize", place);
  window.addEventListener(
    "keydown",
    (e) => {
      if (!ctl?.active) return;
      if (e.key === "Escape") ctl.end();
      else if (e.key === "ArrowRight" || e.key === "Enter") ctl.next();
      else if (e.key === "ArrowLeft") ctl.back();
      else return;
      e.preventDefault();
      e.stopPropagation();
    },
    true,
  );
  ctl = new TourController({
    prepare,
    exists: (sel) => visible(sel) !== null,
    onChange: render,
    onEnd: () => {
      markTourSeen(storage());
      if (tourDemo.scene) {
        tourDemo.scene = null;
        tourMap.redraw();
        tourMap.home();
      }
      for (const id of opened) if (id !== "inspector") closeWindow(id);
      opened = [];
    },
  });
  return ctl;
}

/** Start the tour from step 1 (used by the help icon). */
export function startTour(): void {
  opened = [];
  ensure().start();
}

/** Start automatically on the first visit only. */
export function maybeAutoStartTour(): void {
  if (!tourSeen(storage())) startTour();
}
