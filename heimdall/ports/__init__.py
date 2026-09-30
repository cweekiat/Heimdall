"""Infrastructure ports (interfaces). Adapters in heimdall/adapters implement these.

Swapping an adapter (e.g. MemoryBus -> RedisBus, DuckDBStore -> ClickHouseStore) must not
require changes in detect/, patterns/ or fuse/. See docs/decisions/ADR-001-ports-adapters.md.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from heimdall.geo import BBox, Ident
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
    KeyStatus,
    SourceHealth,
    SourceReliability,
    SourceState,
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
        self,
        start: datetime,
        end: datetime,
        h3_r4: Sequence[str] | None = None,
        *,
        bbox: BBox | None = None,
        ident: Ident | None = None,
    ) -> list[AircraftObs]: ...
    def vessels_window(
        self,
        start: datetime,
        end: datetime,
        h3_r4: Sequence[str] | None = None,
        *,
        bbox: BBox | None = None,
        ident: Ident | None = None,
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
    def notes_for(self, alert_id: str) -> list[AnalystNote]:
        """Notes of an alert in **insertion order** (never by `ts`: writers' clocks differ)."""
        ...

    def record_status_change(self, note: AnalystNote) -> None:
        """Journal an analyst status change (`note.status_to`) and make it the alert's status.

        Stores that derive status from the journal (CompositeStore) only add the note; plain
        stores also write the status onto the stored alert. Replaces duck-typed capability flags.
        """
        ...

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


@runtime_checkable
class SourceControl(Protocol):
    """Runtime on/off control and status of configured sources (task 012a)."""

    def list(self) -> builtins.list[SourceState]: ...

    async def set_enabled(self, key: str, enabled: bool) -> SourceState:
        """Start or stop the source pump and persist the override. Unknown key raises KeyError."""
        ...

    async def restart(self, key: str) -> None:
        """Stop and rebuild a source's connector (e.g. after its API key changed), if enabled.
        Unknown key raises KeyError."""
        ...


@runtime_checkable
class SecretStore(Protocol):
    """API-key storage (ADR-006). Values are write-only from the API's point of view."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def status(self) -> builtins.list[KeyStatus]: ...
