// One global selection/time-window store. Map, queue, board, timeline and inspector all read from
// here and subscribe to changes ("brushing and linking"). Rendering is batched per microtask.
import { api } from "./api";
import type { Alert, AlertDetail, AlertStatus, Aoi, AoiSummary, Cell, SourceHealth, TrackPt } from "./api";

export interface Filters {
  aoi: string;
  kind: string;
  status: string;
  grade: string;
  priority: string;
  group: "none" | "aoi" | "priority";
  q: string;
}

export interface TimeWindow {
  from: number;
  to: number;
}

export interface State {
  alerts: Alert[];
  cells: Cell[];
  aois: Aoi[];
  summary: AoiSummary[];
  health: SourceHealth[];
  selected: string | null; // alert id
  detail: AlertDetail | null;
  tracks: { hex: string; path: TrackPt[] }[];
  selectedAoi: string | null;
  selectedEntity: string | null;
  window: TimeWindow | null; // null = live ("now" lock)
  playing: 0 | 1 | 10 | 60;
  filters: Filters;
  checked: Set<string>; // multi-select in queue
  fly: { lat: number; lon: number; n: number } | null;
  tab: "now" | "why" | "trend" | "log";
  live: boolean;
}

export const state: State = {
  alerts: [],
  cells: [],
  aois: [],
  summary: [],
  health: [],
  selected: null,
  detail: null,
  tracks: [],
  selectedAoi: null,
  selectedEntity: null,
  window: null,
  playing: 0,
  filters: { aoi: "", kind: "", status: "", grade: "", priority: "", group: "none", q: "" },
  checked: new Set(),
  fly: null,
  tab: "now",
  live: false,
};

type Listener = (changed: Set<string>) => void;
const listeners = new Set<Listener>();
let pending = new Set<string>();
let scheduled = false;

export function subscribe(fn: Listener): void {
  listeners.add(fn);
}

export function patch(p: Partial<State>): void {
  Object.assign(state, p);
  for (const k of Object.keys(p)) pending.add(k);
  if (!scheduled) {
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      const changed = pending;
      pending = new Set();
      listeners.forEach((l) => l(changed));
    });
  }
}

export const isOpen = (s: AlertStatus): boolean => s === "new" || s === "acknowledged" || s === "under_review";

// ---------------------------------------------------------------- actions

export async function loadDetail(id: string): Promise<void> {
  const d = await api.alert(id);
  if (state.selected !== id) return;
  // show the details at once; tracks (one request per aircraft) follow when ready
  patch({ detail: d });
  const hexes = [...new Set(d.evidence.flatMap((e) => e.entities))].slice(0, 12);
  const tracks = (
    await Promise.all(hexes.map(async (hex) => ({ hex, path: await api.track(hex).catch(() => [] as TrackPt[]) })))
  ).filter((t) => t.path.length > 1);
  if (state.selected === id) patch({ tracks });
}

export async function selectAlert(id: string | null, fly = true): Promise<void> {
  if (id === null) {
    patch({ selected: null, detail: null, tracks: [] });
    return;
  }
  patch({ selected: id, selectedEntity: null });
  const a = state.alerts.find((x) => x.id === id);
  if (fly && a && a.lat !== null && a.lon !== null) patch({ fly: { lat: a.lat, lon: a.lon, n: Date.now() } });
  await loadDetail(id);
}

export function selectAoi(name: string | null): void {
  const same = state.selectedAoi === name;
  const next = same ? null : name;
  const f = { ...state.filters, aoi: next ?? "" };
  patch({ selectedAoi: next, filters: f });
  const s = state.summary.find((x) => x.name === next);
  if (s) patch({ fly: { lat: s.lat, lon: s.lon, n: Date.now() } });
}

export async function refreshAll(): Promise<void> {
  const [alerts, cells, summary] = await Promise.all([api.alerts(), api.cells(), api.aoiSummary()]);
  patch({ alerts, cells, summary });
  if (state.selected) void loadDetail(state.selected);
}

export async function refreshHealth(): Promise<void> {
  patch({ health: (await api.health()).sources });
}

export async function triage(id: string, status: AlertStatus, reason?: string, until?: string): Promise<string | null> {
  try {
    await api.setStatus(id, status, reason, until);
  } catch (e) {
    return String(e).replace(/^Error: \d+ /, "");
  }
  await refreshAll();
  return null;
}

export async function addNote(id: string, text: string): Promise<string | null> {
  try {
    await api.addNote(id, text);
  } catch (e) {
    return String(e);
  }
  await loadDetail(id);
  return null;
}

export function upsertAlert(a: Alert): void {
  patch({ alerts: [a, ...state.alerts.filter((x) => x.id !== a.id)] });
  if (a.id === state.selected) void loadDetail(a.id);
}
