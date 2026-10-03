// Typed client for the Heimdall API. Mirrors heimdall/schemas.py.

export type AlertStatus = "new" | "acknowledged" | "under_review" | "shelved" | "confirmed" | "resolved" | "dismissed" | "expired";
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
export interface AlertSource {
  connector: string;
  family: string;
  admiralty: Admiralty;
}
export interface Footprint {
  kind: "area_cell" | "single_track";
  description: string;
}
/** Write-only view of a stored API key: never the value, at most its last 4 characters. */
export interface KeyStatus {
  name: string;
  connector: string;
  is_set: boolean;
  last4: string | null;
  origin: "store" | "env" | null;
}
export interface Alert {
  id: string;
  ref?: string | null;
  sources?: AlertSource[];
  footprint?: Footprint | null;
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
  /** RF bands the alert concerns (task 028); absent on older alerts. */
  bands?: string[];
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
  /** 043: alert from a demo pack (read-only, never in the live store). */
  demo?: boolean;
  /** 054: callsign / type / role per affected aircraft (ICAO hex). */
  entity_info?: Record<string, EntityInfo>;
}
export interface EntityInfo {
  flight: string | null;
  registration: string | null;
  type: string | null;
  type_name: string | null;
  role: string | null;
  operator: string | null;
  country: string | null;
  mil: boolean;
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
  /** "user" = drawn in the UI; polygon is [lat, lon] vertices when drawn as a polygon (lat/lon/radius are its bounding circle). */
  origin?: "config" | "user";
  polygon?: [number, number][] | null;
}
export interface UserAoi {
  id: string;
  name: string;
  role: "primary" | "control";
  active: boolean;
  shape: "circle" | "polygon";
  lat: number;
  lon: number;
  radius_nm: number;
  polygon: [number, number][] | null;
}
export interface NewAoi {
  name: string;
  role: "primary" | "control";
  shape: "circle" | "polygon";
  lat?: number;
  lon?: number;
  radius_nm?: number;
  polygon?: [number, number][];
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

export interface GeoFeature {
  type: "Feature";
  geometry: { type: string; coordinates: number[][] };
  properties: Record<string, unknown>;
}
export interface FC<P> {
  type: "FeatureCollection";
  features: { type: "Feature"; geometry: { type: string; coordinates: unknown }; properties: P }[];
  truncated?: boolean;
}
export interface DensityCell {
  h3: string;
  count: number;
  mil: number;
}
export interface Density {
  type: "density";
  res: number;
  total: number;
  cells: DensityCell[];
}
/** /api/traffic/{air,sea} with `zoom`: H3 density cells below zoom 5, positions otherwise. */
export type TrafficResp<P> = FC<P> | Density;
export interface AirProps {
  hex: string;
  flight: string | null;
  registration: string | null;
  type_code: string | null;
  mil: boolean;
  alt_baro: number | null;
  gs: number | null;
  track: number | null;
  on_ground: boolean;
  provider: string;
  providers: string[];
  age_s: number;
  gps_bad: boolean;
  in_alert: boolean;
  squawk?: string | null;
  /** 046: military identity (null for non-military aircraft). */
  role?: string | null;
  country?: string | null;
  operator?: string | null;
  type_name?: string | null;
}
export interface SeaProps {
  mmsi: number;
  name: string | null;
  ship_type: number | null;
  ship_class: string;
  sog: number | null;
  cog: number | null;
  heading: number | null;
  destination: string | null;
  provider: string;
  age_s: number;
  in_alert: boolean;
}
export interface NoticeProps {
  id: string;
  source: string;
  kind: string;
  category: string | null;
  ref: string | null;
  title: string;
  valid_from: string | null;
  valid_to: string | null;
  url: string | null;
  alt_lower_ft: number | null;
  alt_upper_ft: number | null;
  text: string;
}
export interface SourceState {
  key: string;
  label: string;
  domain: string;
  enabled: boolean;
  status: "ok" | "degraded" | "down" | "needs_key" | "disabled" | "off";
  detail: string | null;
  last_ok: string | null;
  records_last_hour: number;
  error_rate: number;
  reliability: string;
  needs_key: boolean;
  /** One plain sentence about the source. */
  about?: string | null;
  /** API key names this source can use (empty = no key). */
  keys: string[];
}
export interface BoardAction extends ActionRec {
  alert: { id: string; title: string; kind: string; severity: string };
  label: string;
  risk: "low" | "high";
}

export interface SatPos {
  norad_id: number;
  name: string;
  sensor: string;
  lat: number;
  lon: number;
  alt_km: number;
  epoch: string;
  /** "Earth observation" | "GNSS" | "Space station" | ... */
  group: string;
  /** [lon, lat, alt_km] every 60 s, oldest first, ending at "now". */
  past: [number, number, number][];
  /** [lon, lat, alt_km] every 60 s from "now". */
  next: [number, number, number][];
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

export interface TrafficOpts {
  since?: number;
  compact?: boolean;
  signal?: AbortSignal;
}
/** since= and fmt=compact are only sent when asked for; servers that predate them ignore both. */
const trafficUrl = (kind: string, maxAge: number, bbox: string, sources: string[], zoom: number, o: TrafficOpts): string =>
  `/api/traffic/${kind}?bbox=${bbox}&zoom=${zoom.toFixed(2)}&max_age_s=${maxAge}${sources.length ? `&sources=${sources.join(",")}` : ""}${o.since !== undefined ? `&since=${o.since}` : ""}${o.compact ? "&fmt=compact" : ""}`;

export const api = {
  clearAlerts: () => post<{ cleared: number }>("/api/alerts/clear"),
  alerts: () => req<Alert[]>("/api/alerts"),
  alert: (id: string) => req<AlertDetail>(`/api/alerts/${encodeURIComponent(id)}`),
  cells: () => req<Cell[]>("/api/cells?res=4"),
  aois: () => req<Aoi[]>("/api/aois"),
  userAois: () => req<UserAoi[]>("/api/aois/user"),
  createAoi: (a: NewAoi) => post<UserAoi>("/api/aois/user", a),
  patchAoi: (id: string, p: Partial<Pick<UserAoi, "name" | "role" | "active">>) =>
    req<UserAoi>(`/api/aois/user/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) }),
  deleteAoi: (id: string) => req<{ deleted: boolean }>(`/api/aois/user/${encodeURIComponent(id)}`, { method: "DELETE" }),
  aoiSummary: () => req<AoiSummary[]>("/api/aois/summary"),
  setStatus: (id: string, status: AlertStatus, reason?: string, shelve_until?: string) =>
    post<{ alert: Alert; note: Note }>(`/api/alerts/${encodeURIComponent(id)}/status`, { status, reason, shelve_until }),
  addNote: (id: string, text: string) => post<Note>(`/api/alerts/${encodeURIComponent(id)}/notes`, { text }),
  uiConfig: () =>
    req<{
      basemap: "openfreemap" | "carto" | "none";
      overlays: string[];
      initial_view?: { lat: number; lon: number; zoom: number };
      banner?: string;
      scenario?: Scenario;
    }>("/api/ui-config"),
  /** 040: demo replays only — jump the replay clock to a recorded moment. */
  replaySeek: (at: string) => post<{ at: string }>("/api/replay/seek", { at }),
  /** 043: precomputed demos served by the live server (static files, no replay). */
  /** 044: shift brief: trends (rule-based) and a local-model summary (this machine only). */
  briefStatus: () => req<{ available: boolean; model: string; reason?: string }>("/api/brief/status"),
  briefTrends: (hours: number) => req<{ trends: string[] }>(`/api/brief/trends?hours=${hours}`),
  /** 053: daily SITREP (24 h) as Markdown, built from Heimdall's own facts. */
  sitrep: () => req<{ generated: string; markdown: string }>("/api/brief/sitrep"),
  briefSummary: (hours: number) =>
    post<{ trends: string[]; narrative: string; model: string; cited: string[]; unsupported: string[] }>("/api/brief/summary", { hours }),
  demoPacks: () => req<{ name: string; title: string; summary: string; n_steps: number }[]>("/api/demo/packs"),
  demoPack: (name: string) => req<Scenario>(`/api/demo/packs/${encodeURIComponent(name)}`),
  health: () => req<{ sources: SourceHealth[]; time: string }>("/api/health"),
  playbook: () => req<PlaybookAction[]>("/api/playbook"),
  track: (hex: string) => req<TrackPt[]>(`/api/tracks/${encodeURIComponent(hex)}`),
  metric: (key: string, metric: string, hours = 24) =>
    req<{ ts: string; value: number }[]>(
      `/api/metrics/${encodeURIComponent(key)}/${metric}?since=${encodeURIComponent(new Date(Date.now() - hours * 3600_000).toISOString())}`,
    ),
  feedback: (id: string, verdict: "confirm" | "dismiss", reason: string) =>
    post(`/api/alerts/${encodeURIComponent(id)}/feedback`, { verdict, reason }),
  propose: (id: string, action_id: string) =>
    post<ActionRec>(`/api/alerts/${encodeURIComponent(id)}/actions`, { action_id }),
  approve: (actionKey: string) => post<ActionRec>(`/api/actions/${encodeURIComponent(actionKey)}/approve`),
  sources: () => req<SourceState[]>("/api/sources"),
  keys: () => req<KeyStatus[]>("/api/keys"),
  // Same-origin fetch: the browser adds the Origin header the server checks. The value is sent once and never kept.
  putKey: (name: string, value: string) =>
    req<{ saved: boolean }>(`/api/keys/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    }),
  deleteKey: (name: string) => req<{ deleted: boolean }>(`/api/keys/${encodeURIComponent(name)}`, { method: "DELETE" }),
  setSource: (key: string, enabled: boolean) => post<SourceState>(`/api/sources/${encodeURIComponent(key)}`, { enabled }),
  air: (bbox: string, sources: string[], zoom: number, o: TrafficOpts = {}) =>
    req<unknown>(trafficUrl("air", 60, bbox, sources, zoom, o), { signal: o.signal }),
  sea: (bbox: string, sources: string[], zoom: number, o: TrafficOpts = {}) =>
    req<unknown>(trafficUrl("sea", 900, bbox, sources, zoom, o), { signal: o.signal }),
  airTrail: (hex: string) => req<GeoFeature>(`/api/traffic/air/${encodeURIComponent(hex)}/trail?min=30`),
  seaTrail: (mmsi: number) => req<GeoFeature>(`/api/traffic/sea/${mmsi}/trail?min=120`),
  notices: () => req<FC<NoticeProps>>("/api/notices"),
  /** P3 layer for one area (GeoJSON, see docs/tasks/023a). Parsed by p3model.parseLayer. */
  layer: (layer: string, aoi: string, hours?: number, signal?: AbortSignal) =>
    req<unknown>(`/api/layers/${encodeURIComponent(layer)}?aoi=${encodeURIComponent(aoi)}${hours ? `&hours=${hours}` : ""}`, { signal }),
  /** GNSS interference day map (task 031): date = YYYY-MM-DD or last24h. Parsed by spectrummodel.parseGnssDay. */
  gnssDay: (date: string, signal?: AbortSignal) => req<unknown>(`/api/gnss/day?date=${encodeURIComponent(date)}`, { signal }),
  satellites: () => req<{ ts: string; satellites: SatPos[] }>("/api/satellites?past_min=30&next_min=100"),
  gnssDays: () => req<{ days: string[]; last24h: boolean }>("/api/gnss/days"),
  cygnssDays: () => req<{ days: string[] }>("/api/cygnss/days"),
  cygnssDay: (date: string) => req<unknown>(`/api/cygnss/day${date ? `?date=${encodeURIComponent(date)}` : ""}`),
  /** Spectrum status board (task 033). Parsed by spectrummodel.parseStatus. */
  spectrumStatus: () => req<unknown>("/api/spectrum/status"),
  /** 039: worldwide military aircraft with identity, roles, countries, theatres. */
  military: () => req<MilitarySummary>("/api/military"),
  /** 056: air force watch (fleet picture per air force). */
  airForces: () => req<{ key: string; name: string; short: string }[]>("/api/airforces"),
  airForce: (key: string, days: number) => req<AirForcePicture>(`/api/airforces/${encodeURIComponent(key)}?days=${days}`),
  /** 051: theatre activity index (components vs each theatre's own normal). */
  theatreIndex: () => req<TheatreIndexResp>("/api/military/theatres/index"),
  /** 049: named military bases worldwide (OpenStreetMap, ODbL). */
  bases: () => req<BasesFC>("/api/military/bases"),
  /** 049: latest headlines, untrusted text (display only). */
  news: (limit = 40) => req<{ items: NewsItem[]; total: number }>(`/api/news?limit=${limit}`),
  /** 047: one military aircraft investigated (identity, type reference, take-off, activity, track). */
  investigate: (hex: string, at?: string) =>
    req<Investigation>(`/api/military/aircraft/${encodeURIComponent(hex)}${at ? `?at=${encodeURIComponent(at)}` : ""}`),
  /** Estimated jammer locations from open GNSS-jamming alerts (GeoJSON ellipses). */
  jammers: () => req<unknown>("/api/jammers"),
  spaceWeather: () =>
    req<{ available: boolean; kp?: number; kp_max_24h?: number; g_scale?: number | null; r_scale?: number | null; s_scale?: number | null; ts?: string }>("/api/spaceweather"),
  /** Receiver coverage per band (task 028): H3 r4 cells with receiver counts over the last hour. */
  coverage: (band: string, bbox?: string) => req<unknown>(`/api/coverage?band=${encodeURIComponent(band)}${bbox ? `&bbox=${bbox}` : ""}`),
  board: (sinceH = 72) => req<BoardAction[]>(`/api/actions?since_h=${sinceH}`),
  reject: (actionKey: string) => post<ActionRec>(`/api/actions/${encodeURIComponent(actionKey)}/reject`),
};

/** 039: one military aircraft (identity from plane-alert-db, ODbL, and the type code). */
export interface MilAircraft {
  hex: string;
  flight: string | null;
  type: string | null;
  type_name: string | null;
  registration: string | null;
  role: string;
  country: string | null;
  operator: string | null;
  category: string | null;
  tags: string[];
  lat: number;
  lon: number;
  alt: number | null;
  on_ground: boolean;
  gs: number | null;
  track: number | null;
  ts: string;
  high_interest: boolean;
  known: boolean;
}
export interface MilTheatre {
  name: string;
  lat: number;
  lon: number;
  radius_km: number;
  radius_nm: number;
  n: number;
  roles: Record<string, number>;
}
export interface MilitarySummary {
  generated: string;
  window_min: number;
  contacts: number;
  airborne: number;
  on_ground: number;
  identified: number;
  roles: Record<string, number>;
  types: { type: string; name: string | null; n: number }[];
  countries: { country: string; n: number }[];
  high_interest: MilAircraft[];
  theatres: MilTheatre[];
  aircraft: MilAircraft[];
  attribution: string;
}

/** 040: a step-by-step demo over a real-data replay (scripts/midnight_hammer.py). */
export interface ScenarioStep {
  when: string;
  title: string;
  brief: string;
  saw: string;
  status: "seen" | "not_visible" | "no_data";
  at: string;
  lat: number;
  lon: number;
  zoom: number;
  examples: string[];
  alert: string | null;
  /** 043: military aircraft at that moment (demo packs only). */
  snapshot?: DemoAircraft[];
}
export interface DemoAircraft {
  hex: string;
  flight: string | null;
  type: string | null;
  type_name: string | null;
  role: string;
  operator: string | null;
  country: string | null;
  lat: number;
  lon: number;
  alt: number | null;
  track: number | null;
  trail: number[][];
}
export interface Scenario {
  title: string;
  summary: string;
  caveat: string;
  sources: string;
  steps: ScenarioStep[];
}

/** 047: GET /api/military/aircraft/{hex}. */
export interface AirfieldRef {
  ident: string;
  name: string;
  country: string;
  military: boolean;
  kind: string;
  lat: number;
  lon: number;
  distance_km: number;
}
export interface TypeReference {
  type: string;
  name: string | null;
  mission: string | null;
  range_km: number | null;
  endurance_h: number | null;
  crew: number | null;
  refuelling: string | null;
  capabilities: string | null;
  source: string | null;
}
export interface Investigation {
  hex: string;
  flight: string | null;
  registration: string | null;
  type: string | null;
  type_name: string | null;
  role: string;
  country: string | null;
  operator: string | null;
  category: string | null;
  tags: string[];
  known: boolean;
  high_interest: boolean;
  reference: TypeReference | null;
  departure: { kind: "took_off" | "first_seen" | "on_ground" | "unknown"; at?: string; lat?: number; lon?: number; airfield?: AirfieldRef | null; text: string };
  activity: { label: string; confidence: "likely" | "possible"; reasons: string[]; context: string[] };
  last: { ts: string; lat: number; lon: number; alt: number | null; gs: number | null };
  /** [lon, lat, alt ft or -1, epoch s] */
  track: [number, number, number, number][];
  flight_from: number;
  airfields_loaded: boolean;
  limits: string;
  attribution: string;
}

/** 049 */
export interface BaseProps {
  id: string;
  name: string;
  kind: string;
  label: string;
  operator: string | null;
  icao: string | null;
}
export interface BasesFC {
  type: "FeatureCollection";
  features: { type: "Feature"; geometry: { type: "Point"; coordinates: [number, number] }; properties: BaseProps }[];
  total: number;
  attribution: string;
}
export interface NewsItem {
  title: string;
  feed: string | null;
  url: string | null;
  ts: string;
  untrusted: true;
}

/** 051 */
export interface TheatreComponent {
  key: string;
  label: string;
  now: number;
  normal: number | null;
  above: boolean;
  why: string;
}
export interface TheatreIdx {
  name: string;
  lat: number;
  lon: number;
  radius_km: number;
  level: number;
  word: string;
  components: TheatreComponent[];
}
export interface TheatreIndexResp {
  generated: string;
  theatres: TheatreIdx[];
  note: string;
}

/** 056 */
export interface AfPlace {
  ident: string | null;
  name: string;
  country: string | null;
  kind: string;
  unit: string | null;
}
export interface AfAircraft {
  hex: string;
  ident: string;
  type: string | null;
  type_name: string | null;
  status: string;
  place: AfPlace | null;
  since: string | null;
  last_seen: string;
  lat: number;
  lon: number;
  flights: number;
}
export interface AfMove {
  hex: string;
  dep_t: string;
  arr_t: string;
  dep: AfPlace | null;
  arr: AfPlace | null;
  hours: number;
}
export interface AirForcePicture {
  key: string;
  name: string;
  short: string;
  days: number;
  fleet_listed: number;
  seen: number;
  status: Record<string, number>;
  types: Record<string, number>;
  aircraft: AfAircraft[];
  movements: AfMove[];
  flights_per_day: { day: string; flights: number }[];
  notes: { level: "info" | "watch"; text: string; hexes: string[] }[];
  not_seen: { hex: string; registration: string | null; type: string | null; type_name: string | null }[];
  limits: string;
  attribution: string;
}
