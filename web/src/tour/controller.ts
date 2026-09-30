// Tour state machine, free of DOM so it can be tested offline. The overlay drives it.
import { TOUR_STEPS } from "./steps.ts";
import type { TourStep } from "./steps.ts";

export const TOUR_FLAG = "heimdall.tour.done";

export interface StorageLike {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
}

/** Storage can throw (private mode, blocked cookies): treat that as "not seen". */
export function tourSeen(store: StorageLike | null): boolean {
  try {
    return store?.getItem(TOUR_FLAG) === "1";
  } catch {
    return false;
  }
}

export function markTourSeen(store: StorageLike | null): void {
  try {
    store?.setItem(TOUR_FLAG, "1");
  } catch {
    /* ignore */
  }
}

export interface TourHooks {
  /** Open windows / select an alert before the anchor is looked up. */
  prepare: (s: TourStep) => void;
  /** True if the anchor element exists right now. */
  exists: (selector: string) => boolean;
  onChange: (s: TourStep | null, index: number, total: number) => void;
  onEnd: () => void;
}

export class TourController {
  index = -1;
  active = false;
  private hooks: TourHooks;
  readonly steps: TourStep[];
  constructor(hooks: TourHooks, steps: TourStep[] = TOUR_STEPS) {
    this.hooks = hooks;
    this.steps = steps;
  }

  private find(from: number, dir: 1 | -1): number {
    for (let i = from; i >= 0 && i < this.steps.length; i += dir) {
      const s = this.steps[i]!;
      try {
        this.hooks.prepare(s);
      } catch {
        /* a failing prepare only skips its step */
      }
      if (this.hooks.exists(s.anchor)) return i;
    }
    return -1;
  }

  private show(i: number): void {
    this.index = i;
    this.hooks.onChange(this.steps[i] ?? null, i, this.steps.length);
  }

  /** Start (or restart) at the first step whose anchor exists. */
  start(): void {
    this.active = true;
    const i = this.find(0, 1);
    if (i < 0) return this.end();
    this.show(i);
  }

  next(): void {
    if (!this.active) return;
    const i = this.find(this.index + 1, 1);
    if (i < 0) return this.end();
    this.show(i);
  }

  back(): void {
    if (!this.active) return;
    const i = this.find(this.index - 1, -1);
    if (i >= 0) this.show(i);
  }

  /** Skip and finish both end the tour. */
  end(): void {
    this.active = false;
    this.index = -1;
    this.hooks.onChange(null, -1, this.steps.length);
    this.hooks.onEnd();
  }
}
