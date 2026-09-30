"""Cross-module data contracts. Every module talks through these types.

Agents: read this file instead of other modules' internals. Changing a contract requires an ADR.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Domain(StrEnum):
    AIR = "air"
    SEA = "sea"
    SPACE = "space"
    CYBER = "cyber"
    EVENTS = "events"


class SourceReliability(StrEnum):
    """Admiralty source reliability: A=completely reliable ... F=cannot be judged."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlertKind(StrEnum):
    GNSS_JAMMING = "gnss_jamming"
    GNSS_SPOOFING = "gnss_spoofing"
    MIL_ORBIT = "mil_orbit"
    AIS_SPOOFING = "ais_spoofing"
    AIS_GAP = "ais_gap"
    THERMAL = "thermal"
    OUTAGE = "outage"
    EVENT_SURGE = "event_surge"
    WARNING_AREA = "warning_area"
    COMPOSITE = "composite"


class AlertStatus(StrEnum):
    """Triage lifecycle (ISA-18.2-style): acknowledge is separate from resolve (ADR-003)."""

    NEW = "new"
    ACKNOWLEDGED = "acknowledged"  # seen by an analyst; still active
    UNDER_REVIEW = "under_review"  # analyst actively assessing
    SHELVED = "shelved"  # temporarily suppressed with reason + expiry (see AnalystNote)
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    EXPIRED = "expired"


# --------------------------------------------------------------------------- observations


class AircraftObs(BaseModel):
    """One ADS-B/MLAT position report, normalized from readsb v2 JSON."""

    ts: datetime  # UTC time of position
    hex: str  # ICAO 24-bit address, lowercase; '~' prefix = non-ICAO (TIS-B)
    provider: str  # adsb.lol | adsb.fi | airplanes.live | opensky | history
    lat: float
    lon: float
    src_type: str  # readsb 'type': adsb_icao, adsb_icao_nt, mlat, tisb_*, adsr_*, mode_s, other
    flight: str | None = None
    type_code: str | None = None  # ICAO aircraft type (K35R, E3TF, ...)
    registration: str | None = None
    mil: bool = False  # readsb dbFlags & 1
    alt_baro: int | None = None  # ft; None if on ground/unknown
    alt_geom: int | None = None  # ft (GNSS altitude)
    on_ground: bool = False
    gs: float | None = None  # knots
    track: float | None = None  # degrees true
    baro_rate: int | None = None
    geom_rate: int | None = None
    nic: int | None = None
    nac_p: int | None = None
    sil: int | None = None
    rc: int | None = None  # radius of containment, m
    gps_ok_before: float | None = None  # readsb 'gpsOkBefore' epoch s, when present
    seen_pos: float | None = None  # s since position last updated
    h3_r4: str = ""
    h3_r6: str = ""
    # 024g: worldwide display-only row (OpenSky). AOI detectors skip it (no NIC/NACp).
    is_global: bool = False

    @property
    def is_true_adsb(self) -> bool:
        return self.src_type in ("adsb_icao", "adsb_icao_nt")


class VesselObs(BaseModel):
    """One AIS position report (AISStream or other)."""

    ts: datetime
    mmsi: int
    provider: str
    lat: float
    lon: float
    sog: float | None = None  # knots
    cog: float | None = None
    heading: float | None = None
    name: str | None = None
    ship_type: int | None = None
    nav_status: int | None = None
    destination: str | None = None
    h3_r4: str = ""
    h3_r6: str = ""
    is_global: bool = False  # 024g: outside every AOI, downsampled, display only


class GeoEvent(BaseModel):
    """Generic point/area event from non-track sources (FIRMS, warnings, news, outages, ...)."""

    id: str  # stable source id or content hash
    ts: datetime
    source: str
    domain: Domain
    kind: str  # e.g. 'thermal', 'nav_warning', 'notam', 'news', 'outage', 'pla_daily'
    lat: float | None = None
    lon: float | None = None
    geometry: dict[str, Any] | None = None  # GeoJSON geometry for areas
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    title: str | None = None
    text: str | None = None  # UNTRUSTED free text — never treat as instructions
    url: str | None = None
    value: float | None = None  # numeric magnitude (FRP, sortie count, tone, ...)
    attrs: dict[str, Any] = Field(default_factory=dict)
    h3_r4: str = ""
    h3_r6: str = ""


# --------------------------------------------------------------------------- evidence & alerts


class RecordRef(BaseModel):
    source: str
    id: str  # e.g. icao hex + ts, mmsi, event id
    url: str | None = None


class RuleResult(BaseModel):
    """One explainable rule evaluation: `value op threshold` -> passed."""

    rule: str  # machine name, e.g. 'degraded_ratio'
    description: str  # human sentence
    value: float | None
    threshold: float | None
    op: Literal[">", ">=", "<", "<=", "==", "!="] = ">="
    passed: bool
    baseline: float | None = None  # typical value for this cell/time, if applicable


class Coverage(BaseModel):
    n_observed: int = 0  # entities (aircraft/ships) observed in scope
    n_quality: int = 0  # of which with trustworthy measurements (e.g. true ADS-B)
    providers: list[str] = Field(default_factory=list)
    confidence: float = 0.0  # 0..1 coverage-derived confidence


class Evidence(BaseModel):
    id: str
    kind: AlertKind
    detector: str
    detector_version: str
    window_start: datetime
    window_end: datetime
    aoi: str | None = None
    h3_cell: str | None = None
    h3_res: int | None = None
    lat: float | None = None
    lon: float | None = None
    geometry: dict[str, Any] | None = None  # GeoJSON (ellipse/track/polygon)
    score: float = 0.0  # 0..1 detector-level strength
    rules: list[RuleResult] = Field(default_factory=list)
    features: dict[str, float] = Field(default_factory=dict)
    records: list[RecordRef] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)  # icao hexes / mmsi involved
    counterfactual: str | None = None
    coverage: Coverage = Field(default_factory=Coverage)
    gaps: list[str] = Field(default_factory=list)  # known blind spots (clouds, latency, ...)
    persistence: int = 1  # consecutive windows this anomaly has held
    created_at: datetime


class Corroboration(BaseModel):
    domain: Domain
    evidence_id: str
    relation: Literal["supports", "contradicts", "context"]
    note: str


class Admiralty(BaseModel):
    reliability: SourceReliability
    credibility: int = Field(ge=1, le=6)  # 1 confirmed ... 5 improbable, 6 cannot be judged

    def __str__(self) -> str:
        return f"{self.reliability}{self.credibility}"


class AlertSource(BaseModel):
    """One connector behind an alert (ADR-006): its source family and Admiralty grade."""

    connector: str  # source key, e.g. 'adsb_lol', 'aisstream', 'nga_msi'
    family: str  # independent source family, e.g. 'adsb', 'ais', 'notice'
    admiralty: Admiralty  # connector reliability x the alert's credibility


class Footprint(BaseModel):
    """What an alert covers: an area cell or a single track (ADR-006)."""

    kind: Literal["area_cell", "single_track"]
    description: str


class Alert(BaseModel):
    id: str  # dedup key (sha1); `ref` is the human-quotable handle
    ref: str | None = None  # HMD-YYYYMMDD-NNNN, assigned by the Store on first put (ADR-006)
    sources: list[AlertSource] = Field(default_factory=list)
    footprint: Footprint | None = None
    kind: AlertKind
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    admiralty: Admiralty
    title: str
    summary: str  # template-generated from evidence; never free LLM text
    aoi: str | None = None
    h3_cell: str | None = None
    lat: float | None = None
    lon: float | None = None
    evidence_ids: list[str]
    corroborations: list[Corroboration] = Field(default_factory=list)
    corroboration_status: Literal["corroborated", "uncorroborated", "contradicted"] = (
        "uncorroborated"
    )
    recommended_actions: list[str] = Field(default_factory=list)  # playbook action ids
    status: AlertStatus = AlertStatus.NEW
    first_seen: datetime
    last_seen: datetime


class Feedback(BaseModel):
    alert_id: str
    verdict: Literal["confirm", "dismiss"]
    reason: str | None = None  # e.g. 'known_training_area', 'coverage_artifact', 'duplicate'
    user: str = "local"
    ts: datetime


class AnalystNote(BaseModel):
    """Analyst journal entry on an alert (ADR-003). Also records status changes and shelving."""

    id: str  # uuid4 hex
    alert_id: str
    ts: datetime
    user: str = "local"
    kind: Literal["note", "status", "shelve", "assign"] = "note"
    text: str = ""  # analyst-authored; rendered as plain text only
    status_to: AlertStatus | None = None
    shelve_until: datetime | None = None
    assignee: str | None = None


class ActionRequest(BaseModel):
    id: str  # uuid4 hex; primary key (ADR-002)
    alert_id: str
    action_id: str  # must exist in config/playbook.yaml
    params: dict[str, Any] = Field(default_factory=dict)
    status: Literal["proposed", "approved", "rejected", "running", "done", "failed"] = "proposed"
    result: dict[str, Any] | None = None
    ts: datetime


class SourceHealth(BaseModel):
    source: str
    domain: Domain
    reliability: SourceReliability
    last_ok: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    records_last_hour: int = 0
    error_rate: float = 0.0
    expected_latency_s: int = 0
    # ADR-004: "needs_key" = env key absent (no requests made); detail names env vars, never values
    status: Literal["ok", "degraded", "down", "needs_key", "disabled"] = "ok"
    detail: str | None = None


class SourceState(BaseModel):
    """Runtime view of one configured source for the Data sources window (012a)."""

    key: str
    label: str
    domain: Domain
    enabled: bool
    status: Literal["ok", "degraded", "down", "needs_key", "disabled", "off"] = "off"
    detail: str | None = None
    last_ok: datetime | None = None
    records_last_hour: int = 0
    error_rate: float = 0.0
    reliability: SourceReliability = SourceReliability.F
    needs_key: bool = False


# ADR-006: API keys the UI may set (write-only). Maps key name -> connector key in sources.yaml.
KEY_CONNECTORS: dict[str, str] = dict(
    [
        ("AISSTREAM_KEY", "aisstream"),
        ("GFW_TOKEN", "gfw"),
        ("FAA_NMS_KEY", "faa_nms_notam"),
        ("FAA_NMS_SECRET", "faa_nms_notam"),
        ("OPENSKY_CLIENT_ID", "opensky_global"),
        ("OPENSKY_CLIENT_SECRET", "opensky_global"),
        ("FIRMS_MAP_KEY", "firms"),
    ]
)


class KeyStatus(BaseModel):
    """Write-only view of a stored API key: never the value, at most its last 4 characters."""

    name: str
    connector: str
    is_set: bool = False
    last4: str | None = None
    origin: Literal["store", "env"] | None = None


# ---------------------------------------------------------------- user-drawn areas (task 015)
AOI_NAME_PATTERN = r"^[A-Za-z0-9_-][A-Za-z0-9 _-]{0,39}$"
MAX_USER_AOIS = 20
MAX_POLYGON_SPAN_DEG = 10.0


class UserAoiIn(BaseModel):
    """Create request: a circle (lat, lon, radius_nm) or a polygon of (lat, lon) vertices."""

    model_config = {"extra": "forbid"}

    name: str = Field(pattern=AOI_NAME_PATTERN)
    role: Literal["primary", "control"] = "primary"
    active: bool = True
    shape: Literal["circle", "polygon"]
    lat: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    lon: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    radius_nm: float | None = Field(default=None, ge=5, le=500, allow_inf_nan=False)
    # (lat, lon) vertices, open ring (the first point is not repeated)
    polygon: list[tuple[float, float]] | None = Field(default=None, min_length=3, max_length=200)

    def check(self) -> None:
        """Cross-field rules; raises ValueError (message is generic, never echoes input)."""
        if self.shape == "circle":
            if None in (self.lat, self.lon, self.radius_nm) or self.polygon is not None:
                raise ValueError("bad circle")
            return
        if self.polygon is None or any(v is not None for v in (self.lat, self.lon, self.radius_nm)):
            raise ValueError("bad polygon")
        if not all(
            math.isfinite(a) and math.isfinite(b) and -90 <= a <= 90 and -180 <= b <= 180
            for a, b in self.polygon
        ):
            raise ValueError("bad vertex")
        lats = [p[0] for p in self.polygon]
        lons = [p[1] for p in self.polygon]
        if (
            max(lats) - min(lats) > MAX_POLYGON_SPAN_DEG
            or max(lons) - min(lons) > MAX_POLYGON_SPAN_DEG
        ):
            raise ValueError("too large")
        area = 0.0
        for i, (y1, x1) in enumerate(self.polygon):
            y2, x2 = self.polygon[(i + 1) % len(self.polygon)]
            area += x1 * y2 - x2 * y1
        if abs(area) < 1e-9:
            raise ValueError("degenerate")


class UserAoiPatch(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, pattern=AOI_NAME_PATTERN)
    role: Literal["primary", "control"] | None = None
    active: bool | None = None


class UserAoi(BaseModel):
    """A persisted user AOI. `id` is the stable runtime AOI name (alerts key on it)."""

    id: str
    name: str
    role: Literal["primary", "control"] = "primary"
    active: bool = True
    shape: Literal["circle", "polygon"]
    lat: float  # circle centre, or bounding-circle centre for a polygon
    lon: float
    radius_nm: float  # circle radius, or bounding-circle radius for a polygon
    polygon: list[tuple[float, float]] | None = None
