"""Infrastructure ports (interfaces). Adapters in heimdall/adapters implement these.

Swapping an adapter (e.g. MemoryBus -> RedisBus, DuckDBStore -> ClickHouseStore) must not
require changes in detect/, patterns/ or fuse/. See docs/decisions/ADR-001-ports-adapters.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from heimdall.schemas import (
    ActionRequest,
    AircraftObs,
    Alert,
    AlertStatus,
    AnalystNote,
    Domain,
    Evidence,
    Feedback,
    GeoEvent,
    SourceHealth,
    SourceReliability,
    VesselObs,
)


@runtime_checkable
class Bus(Protocol):
    """Topic pub/sub. Topics: obs.air, obs.sea, events, evidence, alerts, health."""

    async def publish(self, topic: str, batch: Sequence[BaseModel]) -> None: ...

    def subscribe(self, topic: str) -> AsyncIterator[Sequence[BaseModel]]: ...

    async def close(self) -> None: ...


@runtime_checkable
class Lake(Protocol):
    """Append-only columnar source of truth (Parquet locally, S3/Iceberg when hosted)."""

    def append(self, dataset: str, rows: Sequence[BaseModel]) -> int: ...

    def paths(self, dataset: str, start: datetime, end: datetime) -> list[str]: ...


@runtime_checkable
class Store(Protocol):
    """Hot queryable store for windows, evidence, alerts, feedback and health."""

    # observations
    def insert_aircraft(self, rows: Sequence[AircraftObs]) -> int: ...
    def insert_vessels(self, rows: Sequence[VesselObs]) -> int: ...
    def insert_events(self, rows: Sequence[GeoEvent]) -> int:
        """Idempotent on GeoEvent.id, last write wins (ADR-004); the Lake keeps every revision."""
        ...

    def aircraft_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[AircraftObs]: ...
    def vessels_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[VesselObs]: ...
    def events_window(
        self, start: datetime, end: datetime, kinds: Sequence[str] | None = None
    ) -> list[GeoEvent]: ...

    # evidence & alerts
    def upsert_evidence(self, ev: Evidence) -> None: ...
    def get_evidence(self, ids: Sequence[str]) -> list[Evidence]: ...
    def evidence_history(self, h3_cell: str, kind: str, since: datetime) -> list[Evidence]: ...
    def upsert_alert(self, alert: Alert) -> None: ...
    def get_alert(self, alert_id: str) -> Alert | None: ...
    def list_alerts(
        self,
        since: datetime | None = None,
        status: Sequence[AlertStatus] | None = None,
        aoi: str | None = None,
        limit: int = 200,
    ) -> list[Alert]: ...

    # analyst loop
    def add_feedback(self, fb: Feedback) -> None: ...
    def feedback_for(self, alert_id: str) -> list[Feedback]: ...
    def add_note(self, note: AnalystNote) -> None: ...
    def notes_for(self, alert_id: str) -> list[AnalystNote]: ...
    def upsert_action(self, action: ActionRequest) -> None: ...
    def actions_for(self, alert_id: str) -> list[ActionRequest]: ...
    def get_action(self, action_id: str) -> ActionRequest | None: ...

    # baselines: generic (key, metric) -> time series of values
    def record_metric(self, key: str, metric: str, ts: datetime, value: float) -> None: ...
    def metric_history(
        self, key: str, metric: str, since: datetime
    ) -> list[tuple[datetime, float]]: ...

    # health
    def upsert_health(self, h: SourceHealth) -> None: ...
    def list_health(self) -> list[SourceHealth]: ...

    def close(self) -> None: ...


@runtime_checkable
class Connector(Protocol):
    """A data source. Polling or streaming; yields normalized, validated batches."""

    name: str
    domain: Domain
    reliability: SourceReliability
    expected_latency_s: int

    def stream(self) -> AsyncIterator[Sequence[BaseModel]]: ...

    def health(self) -> SourceHealth:
        """ADR-004: current health; a keyed connector without its key reports needs_key."""
        ...

    async def aclose(self) -> None: ...
