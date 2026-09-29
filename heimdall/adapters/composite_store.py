"""Store that reads from a (read-only) main store and keeps analyst writes in a separate store.

DuckDB is single-writer and the ingest process owns the main file, so the API must never write
there. Analyst state (feedback, actions, alert status changes) lives in `analyst`; alerts read
back through this class are overlaid with any analyst-modified copy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from heimdall.ports import Store
from heimdall.schemas import (
    ActionRequest,
    AircraftObs,
    Alert,
    AlertStatus,
    Evidence,
    Feedback,
    GeoEvent,
    SourceHealth,
    VesselObs,
)


class CompositeStore:
    def __init__(self, read: Store, analyst: Store) -> None:
        self.read = read
        self.analyst = analyst

    # ---- pure reads / pipeline-owned data: main store
    def insert_aircraft(self, rows: Sequence[AircraftObs]) -> int:
        return self.read.insert_aircraft(rows)

    def insert_vessels(self, rows: Sequence[VesselObs]) -> int:
        return self.read.insert_vessels(rows)

    def insert_events(self, rows: Sequence[GeoEvent]) -> int:
        return self.read.insert_events(rows)

    def aircraft_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[AircraftObs]:
        return self.read.aircraft_window(start, end, h3_r4)

    def vessels_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[VesselObs]:
        return self.read.vessels_window(start, end, h3_r4)

    def events_window(
        self, start: datetime, end: datetime, kinds: Sequence[str] | None = None
    ) -> list[GeoEvent]:
        return self.read.events_window(start, end, kinds)

    def upsert_evidence(self, ev: Evidence) -> None:
        self.read.upsert_evidence(ev)

    def get_evidence(self, ids: Sequence[str]) -> list[Evidence]:
        return self.read.get_evidence(ids)

    def evidence_history(self, h3_cell: str, kind: str, since: datetime) -> list[Evidence]:
        return self.read.evidence_history(h3_cell, kind, since)

    def record_metric(self, key: str, metric: str, ts: datetime, value: float) -> None:
        self.read.record_metric(key, metric, ts, value)

    def metric_history(
        self, key: str, metric: str, since: datetime
    ) -> list[tuple[datetime, float]]:
        return self.read.metric_history(key, metric, since)

    def upsert_health(self, h: SourceHealth) -> None:
        self.read.upsert_health(h)

    def list_health(self) -> list[SourceHealth]:
        return self.read.list_health()

    # ---- alerts: main store overlaid with analyst-modified copies
    def upsert_alert(self, alert: Alert) -> None:
        self.analyst.upsert_alert(alert)

    def get_alert(self, alert_id: str) -> Alert | None:
        return self.analyst.get_alert(alert_id) or self.read.get_alert(alert_id)

    def list_alerts(
        self,
        since: datetime | None = None,
        status: Sequence[AlertStatus] | None = None,
        aoi: str | None = None,
        limit: int = 200,
    ) -> list[Alert]:
        merged = {a.id: a for a in self.read.list_alerts(since, None, aoi, limit)}
        for a in self.analyst.list_alerts(since, None, aoi, limit):
            merged[a.id] = a
        out = [a for a in merged.values() if status is None or a.status in status]
        out.sort(key=lambda a: a.last_seen, reverse=True)
        return out[:limit]

    # ---- analyst loop: analyst store
    def add_feedback(self, fb: Feedback) -> None:
        self.analyst.add_feedback(fb)

    def feedback_for(self, alert_id: str) -> list[Feedback]:
        return self.analyst.feedback_for(alert_id)

    def upsert_action(self, action: ActionRequest) -> None:
        self.analyst.upsert_action(action)

    def actions_for(self, alert_id: str) -> list[ActionRequest]:
        return self.analyst.actions_for(alert_id)

    def close(self) -> None:
        self.read.close()
        self.analyst.close()
