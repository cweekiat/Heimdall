// Typed client for the Heimdall API. Mirrors heimdall/schemas.py.

export type AlertStatus = "new" | "acknowledged" | "under_review" | "shelved" | "confirmed" | "dismissed" | "expired";
export interface Note {
  id: string;
  alert_id: string;
  ts: string;
  user: string;
  kind: "note" | "status" | "shelve" | "assign";
  text: string;
  status_to: AlertStatus | null;
  shelve_until: string | null;
}
export interface AoiSummary {
  name: string;
  label: string | null;
  role: string;
  lat: number;
  lon: number;
  open_total: number;
  open_by_kind: Record<string, number>;
  max_severity: string | null;
  ratio_now: number | null;
  ratio_median: number | null;
  coverage: number;
  last_change: string | null;
  spark: (number | null)[];
}
export interface Admiralty {
  reliability: string;
  credibility: number;
}
export interface Corroboration {
  domain: string;
  evidence_id: string;
  relation: "supports" | "contradicts" | "context";
  note: string;
}
export interface Alert {
  id: string;
  kind: string;
  severity: "info" | "low" | "medium" | "high" | "critical";
  confidence: number;
  admiralty: Admiralty;
  title: string;
  summary: string;
  aoi: string | null;
  h3_cell: string | null;
  lat: number | null;
  lon: number | null;
  evidence_ids: string[];
  corroborations: Corroboration[];
  corroboration_status: "corroborated" | "uncorroborated" | "contradicted";
  recommended_actions: string[];
  status: AlertStatus;
  first_seen: string;
  last_seen: string;
}
export interface Rule {
  rule: string;
  description: string;
  value: number | null;
  threshold: number | null;
  op: string;
  passed: boolean;
  baseline: number | null;
}
export interface Evidence {
  id: string;
  kind: string;
  detector: string;
  detector_version: string;
  window_start: string;
  window_end: string;
  aoi: string | null;
  h3_cell: string | null;
  lat: number | null;
  lon: number | null;
  geometry: { type: string; coordinates: unknown } | null;
  score: number;
  rules: Rule[];
  features: Record<string, number>;
  records: { source: string; id: string; url: string | null }[];
  entities: string[];
  counterfactual: string | null;
  coverage: { n_observed: number; n_quality: number; providers: string[]; confidence: number };
  gaps: string[];
  persistence: number;
}
export interface PlaybookAction {
  id: string;
  label: string;
  risk: "low" | "high";
  applies_to: string[];
}
export interface ActionRec {
  id: string;
  alert_id: string;
  action_id: string;
  status: string;
  result: Record<string, unknown> | null;
  ts: string;
}
export interface AlertDetail {
  alert: Alert;
  evidence: Evidence[];
  feedback: { verdict: string; reason: string | null; ts: string; user: string }[];
  notes: Note[];
  actions: ActionRec[];
  playbook: PlaybookAction[];
}
export interface Cell {
  h3: string;
  ratio: number | null;
  score: number;
  kind: string;
  n_observed: number;
  n_quality: number;
  ts: string;
  evidence_id: string;
}
export interface Aoi {
  name: string;
  role: string;
  lat: number;
  lon: number;
  radius_nm: number;
  label?: string | null;
}
export interface SourceHealth {
  source: string;
  domain: string;
  reliability: string;
  last_ok: string | null;
  last_error: string | null;
  last_error_at: string | null;
  records_last_hour: number;
  error_rate: number;
  expected_latency_s: number;
}
export interface TrackPt {
  ts: string;
  lat: number;
  lon: number;
  alt: number | null;
  gs: number | null;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return (await r.json()) as T;
}
const post = <T>(path: string, body?: unknown) =>
  req<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export const api = {
  alerts: () => req<Alert[]>("/api/alerts"),
  alert: (id: string) => req<AlertDetail>(`/api/alerts/${encodeURIComponent(id)}`),
  cells: () => req<Cell[]>("/api/cells?res=4"),
  aois: () => req<Aoi[]>("/api/aois"),
  aoiSummary: () => req<AoiSummary[]>("/api/aois/summary"),
  setStatus: (id: string, status: AlertStatus, reason?: string, shelve_until?: string) =>
    post<{ alert: Alert; note: Note }>(`/api/alerts/${encodeURIComponent(id)}/status`, { status, reason, shelve_until }),
  addNote: (id: string, text: string) => post<Note>(`/api/alerts/${encodeURIComponent(id)}/notes`, { text }),
  uiConfig: () => req<{ basemap: "openfreemap" | "carto" | "none"; overlays: string[] }>("/api/ui-config"),
  health: () => req<{ sources: SourceHealth[]; time: string }>("/api/health"),
  playbook: () => req<PlaybookAction[]>("/api/playbook"),
  track: (hex: string) => req<TrackPt[]>(`/api/tracks/${encodeURIComponent(hex)}`),
  metric: (key: string, metric: string) =>
    req<{ ts: string; value: number }[]>(`/api/metrics/${key}/${metric}`),
  feedback: (id: string, verdict: "confirm" | "dismiss", reason: string) =>
    post(`/api/alerts/${encodeURIComponent(id)}/feedback`, { verdict, reason }),
  propose: (id: string, action_id: string) =>
    post<ActionRec>(`/api/alerts/${encodeURIComponent(id)}/actions`, { action_id }),
  approve: (actionKey: string) => post<ActionRec>(`/api/actions/${encodeURIComponent(actionKey)}/approve`),
  reject: (actionKey: string) => post<ActionRec>(`/api/actions/${encodeURIComponent(actionKey)}/reject`),
};
