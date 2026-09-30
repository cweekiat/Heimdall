// Window manager: floating, draggable, resizable windows over the full-screen map.
// Below 768 px windows become bottom sheets (tap the title bar handle or swipe it).
// Layout persists in localStorage under hm.wm.v1 (geometry only).
import { h } from "./dom";
import { icon, iconButton } from "./icons";
import type { IconName } from "./icons";

export interface WinSpec {
  id: string;
  title: string;
  icon: IconName;
  /** Persistent body element. Renderers may replaceChildren() it at any time. */
  body?: HTMLElement;
  w?: number;
  h?: number;
  /** Default position; negative values anchor to the right / bottom edge. */
  x?: number;
  y?: number;
  minW?: number;
  minH?: number;
  onOpen?: () => void;
  onClose?: () => void;
}

interface Geo {
  x: number;
  y: number;
  w: number;
  h: number;
}
interface Win {
  spec: WinSpec;
  el: HTMLElement;
  body: HTMLElement;
  titleEl: HTMLElement;
  geo: Geo;
  open: boolean;
  min: boolean;
}

const KEY = "hm.wm.v1";
const SNAP = 12;
const KEEP = 40; // px of title bar that must stay on screen
const TITLE_H = 32;
export const EDGE_LEFT = 44; // toolbar width
export const EDGE_TOP = 32; // status strip height

const wins = new Map<string, Win>();
const listeners: (() => void)[] = [];
let layer: HTMLElement | null = null;
let zTop = 20;
let focused: string | null = null;
let saved: Record<string, Geo> = {};

try {
  saved = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Record<string, Geo>;
} catch {
  saved = {};
}
const persist = (): void => {
  try {
    localStorage.setItem(KEY, JSON.stringify(saved));
  } catch {
    /* storage unavailable: layout just will not persist */
  }
};

const vw = (): number => window.innerWidth;
const vh = (): number => window.innerHeight;
export const isSheet = (): boolean => window.innerWidth < 768;
const notify = (): void => listeners.forEach((f) => f());

function clamp(g: Geo): Geo {
  const w = Math.min(g.w, vw() - 8);
  const hh = Math.min(g.h, vh() - EDGE_TOP - 8);
  const x = Math.min(vw() - KEEP, Math.max(KEEP - w, g.x));
  const y = Math.min(vh() - TITLE_H, Math.max(0, g.y));
  return { x, y, w, h: hh };
}

function snap(g: Geo): Geo {
  let { x, y } = g;
  for (const c of [EDGE_LEFT, 0, vw() - g.w]) if (Math.abs(x - c) <= SNAP) x = c;
  if (Math.abs(x + g.w - vw()) <= SNAP) x = vw() - g.w;
  for (const c of [EDGE_TOP, 0, vh() - g.h]) if (Math.abs(y - c) <= SNAP) y = c;
  if (Math.abs(y + g.h - vh()) <= SNAP) y = vh() - g.h;
  return { ...g, x, y };
}

function defaultGeo(s: WinSpec): Geo {
  const w = Math.min(s.w ?? 360, vw() - 8);
  const hh = Math.min(s.h ?? 420, vh() - EDGE_TOP - 8);
  const x = s.x === undefined ? EDGE_LEFT + 8 : s.x < 0 ? vw() + s.x - w : s.x;
  const y = s.y === undefined ? EDGE_TOP + 8 : s.y < 0 ? vh() + s.y - hh : s.y;
  return clamp({ x, y, w, h: hh });
}

function apply(w: Win): void {
  const el = w.el;
  if (isSheet()) {
    el.style.left = el.style.top = el.style.width = el.style.height = "";
    return;
  }
  const g = clamp(w.geo);
  w.geo = g;
  el.style.left = `${g.x}px`;
  el.style.top = `${g.y}px`;
  el.style.width = `${g.w}px`;
  el.style.height = w.min ? "" : `${g.h}px`;
}

function store(w: Win): void {
  saved[w.spec.id] = { ...w.geo };
  persist();
}

function raise(w: Win): void {
  w.el.style.zIndex = String(++zTop);
  focused = w.spec.id;
  wins.forEach((o) => o.el.classList.toggle("focused", o === w));
  notify();
}

function pointerDrag(target: HTMLElement, start: (e: PointerEvent) => ((dx: number, dy: number) => void) | null, done?: () => void): void {
  target.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || isSheet()) return;
    if ((e.target as HTMLElement).closest("button")) return;
    const move = start(e);
    if (!move) return;
    e.preventDefault();
    const x0 = e.clientX;
    const y0 = e.clientY;
    target.setPointerCapture(e.pointerId);
    const onMove = (ev: PointerEvent): void => move(ev.clientX - x0, ev.clientY - y0);
    const onUp = (): void => {
      target.removeEventListener("pointermove", onMove);
      target.removeEventListener("pointerup", onUp);
      target.removeEventListener("pointercancel", onUp);
      done?.();
    };
    target.addEventListener("pointermove", onMove);
    target.addEventListener("pointerup", onUp);
    target.addEventListener("pointercancel", onUp);
  });
}

/** Register a window (initially closed). Idempotent per id. */
export function createWindow(spec: WinSpec): HTMLElement {
  const existing = wins.get(spec.id);
  if (existing) return existing.body;
  if (!layer) {
    layer = h("div", { id: "wm", "aria-label": "Windows" });
    document.body.append(layer);
  }
  const body = spec.body ?? h("div", {});
  body.classList.add("win-body");
  const titleEl = h("span", { class: "win-title" }, spec.title);
  const bar = h("div", { class: "win-bar" });
  const el = h("section", { class: "win", role: "dialog", "aria-label": spec.title, tabindex: "-1", "data-win": spec.id, hidden: true });
  const w: Win = { spec, el, body, titleEl, geo: saved[spec.id] ? clamp(saved[spec.id]!) : defaultGeo(spec), open: false, min: false };
  bar.append(
    h("span", { class: "win-grip", "aria-hidden": "true" }),
    icon(spec.icon),
    titleEl,
    h("span", { class: "win-spacer" }),
    iconButton("minimise", "Minimise", () => setMin(w, !w.min), { cls: "win-btn" }),
    iconButton("close", "Close", () => closeWindow(spec.id), { cls: "win-btn" }),
  );
  el.append(bar, body);
  for (const dir of ["n", "s", "e", "w", "ne", "nw", "se", "sw"]) {
    const hd = h("div", { class: `win-rs rs-${dir}`, "data-dir": dir });
    el.append(hd);
    let g0: Geo = w.geo;
    pointerDrag(
      hd,
      () => {
        if (w.min) return null;
        g0 = { ...w.geo };
        return (dx, dy) => {
          const minW = spec.minW ?? 240;
          const minH = spec.minH ?? 120;
          let { x, y, w: ww, h: hh } = g0;
          if (dir.includes("e")) ww = Math.max(minW, g0.w + dx);
          if (dir.includes("s")) hh = Math.max(minH, g0.h + dy);
          if (dir.includes("w")) {
            ww = Math.max(minW, g0.w - dx);
            x = g0.x + g0.w - ww;
          }
          if (dir.includes("n")) {
            hh = Math.max(minH, g0.h - dy);
            y = g0.y + g0.h - hh;
          }
          w.geo = clamp({ x, y, w: ww, h: hh });
          apply(w);
        };
      },
      () => store(w),
    );
  }
  pointerDrag(
    bar,
    () => {
      const g0 = { ...w.geo };
      raise(w);
      return (dx, dy) => {
        w.geo = clamp(snap({ ...g0, x: g0.x + dx, y: g0.y + dy }));
        apply(w);
      };
    },
    () => store(w),
  );
  bar.addEventListener("dblclick", (e) => {
    if (!(e.target as HTMLElement).closest("button")) setMin(w, !w.min);
  });
  // bottom-sheet gestures: tap or swipe the title bar
  let sy = 0;
  bar.addEventListener("touchstart", (e) => (sy = e.touches[0]!.clientY), { passive: true });
  bar.addEventListener("touchend", (e) => {
    if (!isSheet()) return;
    const dy = e.changedTouches[0]!.clientY - sy;
    if (Math.abs(dy) > 40) setMin(w, dy > 0);
  });
  bar.addEventListener("click", (e) => {
    if (isSheet() && !(e.target as HTMLElement).closest("button")) setMin(w, !w.min);
  });
  el.addEventListener("pointerdown", () => raise(w), true);
  layer.append(el);
  wins.set(spec.id, w);
  apply(w);
  return body;
}

function setMin(w: Win, min: boolean): void {
  w.min = min;
  w.el.classList.toggle("min", min);
  apply(w);
}

export function openWindow(id: string): void {
  const w = wins.get(id);
  if (!w) return;
  if (isSheet()) wins.forEach((o) => o !== w && o.open && closeWindow(o.spec.id));
  const was = w.open;
  w.open = true;
  w.el.hidden = false;
  setMin(w, false);
  raise(w);
  if (!was) w.spec.onOpen?.();
  notify();
}

export function closeWindow(id: string): void {
  const w = wins.get(id);
  if (!w || !w.open) return;
  w.open = false;
  w.el.hidden = true;
  if (focused === id) focused = null;
  w.spec.onClose?.();
  notify();
}

export function toggleWindow(id: string): void {
  if (isOpen(id)) closeWindow(id);
  else openWindow(id);
}
export const isOpen = (id: string): boolean => wins.get(id)?.open ?? false;
export const getBody = (id: string): HTMLElement | null => wins.get(id)?.body ?? null;
export const focusedId = (): string | null => (focused && isOpen(focused) ? focused : null);
export function setTitle(id: string, title: string): void {
  const w = wins.get(id);
  if (!w) return;
  w.titleEl.textContent = title;
  w.el.setAttribute("aria-label", title);
}
/** Position an open or closed window (used to place the inspector beside a selection). */
export function placeWindow(id: string, x: number, y: number): void {
  const w = wins.get(id);
  if (!w) return;
  w.geo = clamp({ ...w.geo, x, y });
  apply(w);
}
export const hasSavedGeometry = (id: string): boolean => Boolean(saved[id]);
/** Close the focused window (Esc). Returns true if one was closed. */
export function closeFocused(): boolean {
  const id = focusedId();
  if (!id) return false;
  closeWindow(id);
  return true;
}
/** Subscribe to open/close/focus changes (toolbar highlights). */
export function onWmChange(fn: () => void): void {
  listeners.push(fn);
}
export function resetLayout(): void {
  saved = {};
  persist();
  wins.forEach((w) => {
    w.geo = defaultGeo(w.spec);
    setMin(w, false);
    apply(w);
  });
}

document.addEventListener("keydown", (e) => {
  if (!e.altKey || !e.key.startsWith("Arrow")) return;
  const id = focusedId();
  const w = id ? wins.get(id) : undefined;
  if (!w || isSheet()) return;
  e.preventDefault();
  const d = e.shiftKey ? 80 : 20;
  const dx = e.key === "ArrowLeft" ? -d : e.key === "ArrowRight" ? d : 0;
  const dy = e.key === "ArrowUp" ? -d : e.key === "ArrowDown" ? d : 0;
  w.geo = clamp({ ...w.geo, x: w.geo.x + dx, y: w.geo.y + dy });
  apply(w);
  store(w);
});

window.addEventListener("resize", () => wins.forEach(apply));
