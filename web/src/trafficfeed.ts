// One live dataset (air or sea, points or density): fetches a padded box, merges by id, drops
// stale responses (sequence number) and aborts requests superseded by a new view. Pure of DOM/deck.
import { boxInside, decode, EntityStore, fmtBox, padBox } from "./trafficdata.ts";
import type { Box, DensityRow } from "./trafficdata.ts";

type Pt = { lon: number; lat: number };

export interface FeedReq {
  bbox: string;
  zoom: number;
  since?: number;
  compact: boolean;
  signal: AbortSignal;
}
export type Fetcher = (r: FeedReq) => Promise<unknown>;

const sameCells = (a: DensityRow[], b: DensityRow[]): boolean =>
  a.length === b.length && a.every((c, i) => c.h3 === b[i]!.h3 && c.count === b[i]!.count && c.mil === b[i]!.mil);

export class Feed<T extends Pt> {
  readonly store: EntityStore<T>;
  cells: DensityRow[] = [];
  truncated = false;
  total: number | null = null;
  private fetchBox: Box | null = null;
  private sig = "";
  private version: number | null = null;
  private compact = true; // try it; switch off when the server answers with plain GeoJSON
  private seq = 0;
  private ctl: AbortController | null = null;
  private inflight: string | null = null;

  readonly kind: "points" | "density";
  private fetcher: Fetcher;
  private now: () => number;

  constructor(kind: "points" | "density", key: (r: T) => string, maxAgeMs: number, fetcher: Fetcher, now: () => number = Date.now) {
    this.kind = kind;
    this.fetcher = fetcher;
    this.now = now;
    this.store = new EntityStore<T>(key, maxAgeMs);
  }

  /** Cancel any request in flight (the caller changed what it wants). */
  cancel(): void {
    this.seq++;
    this.ctl?.abort();
    this.ctl = this.inflight = null;
  }

  clear(): boolean {
    this.cancel();
    const had = this.store.size > 0 || this.cells.length > 0 || this.truncated;
    this.store.clear();
    this.cells = [];
    this.truncated = false;
    this.total = null;
    this.fetchBox = this.version = null;
    return had;
  }

  /** Fetch for the current view. `sig` names everything but the box (sources, zoom class). True if data changed. */
  async poll(view: Box, zoom: number, sig: string): Promise<boolean> {
    const sigSame = sig === this.sig;
    const covered = !!this.fetchBox && sigSame && boxInside(view, this.fetchBox);
    const box = covered ? this.fetchBox! : padBox(view);
    const since = covered && this.version !== null ? this.version : undefined;
    const key = `${fmtBox(box)}|${sig}|${since ?? "-"}`;
    if (this.inflight === key) return false; // same request already on its way
    this.ctl?.abort();
    const ctl = (this.ctl = new AbortController());
    this.inflight = key;
    const my = ++this.seq;
    let resp: unknown;
    try {
      resp = await this.fetcher({ bbox: fmtBox(box), zoom, since, compact: this.compact && this.kind === "points", signal: ctl.signal });
    } catch {
      if (my === this.seq) this.inflight = null; // keep the last picture
      return my === this.seq ? this.store.expire(this.now()) : false;
    }
    if (my !== this.seq) return false; // a newer view superseded this response
    this.inflight = null;
    const d = decode<T>(resp, since !== undefined);
    if (this.kind === "points" && this.compact && !(resp as { cols?: unknown; columns?: unknown } | null)?.cols && !(resp as { columns?: unknown } | null)?.columns) this.compact = false;
    const now = this.now();
    let changed = false;
    if (d.kind === "density") {
      if (this.kind === "density" && !sameCells(this.cells, d.cells)) {
        this.cells = d.cells;
        changed = true;
      }
    } else if (this.kind === "points") {
      changed = d.delta ? this.store.applyDelta(d.rows, d.removed, now) : this.store.replaceFull(d.rows, now, box, sigSame ? this.fetchBox : null);
    }
    if (this.truncated !== d.truncated || this.total !== d.total) changed = true;
    this.truncated = d.truncated;
    this.total = d.total;
    this.fetchBox = box;
    this.sig = sig;
    this.version = d.version;
    return changed;
  }
}
