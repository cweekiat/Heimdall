"""Cross-module data contracts. Every module talks through these types.

Agents: read this file instead of other modules' internals. Changing a contract requires an ADR.
"""

from __future__ import annotations

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


class Alert(BaseModel):
    id: str
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
