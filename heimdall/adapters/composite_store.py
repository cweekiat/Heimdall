"""Store that reads from a (read-only) main store and keeps analyst writes in a separate store.

DuckDB is single-writer and the ingest process owns the main file, so the API must never write
there. Analyst state (feedback, actions) lives in `analyst`. Alerts are never copied: only a
status override (alert_id -> status) is kept and applied over the pipeline's current Alert, so
pipeline updates (last_seen, confidence, evidence) always show through. Overrides survive restarts
because they are re-derived from the latest analyst feedback verdict.
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

_VERDICT_STATUS = {"confirm": AlertStatus.CONFIRMED, "dismiss": AlertStatus.DISMISSED}


class CompositeStore:
    def __init__(self, read: Store, analyst: Store) -> None:
        self.read = read
        self.analyst = analyst
        self._overrides: dict[str, AlertStatus] = {}

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

    # ---- alerts: pipeline alert + analyst status override
    def _override(self, alert_id: str) -> AlertStatus | None:
        if alert_id not in self._overrides:
            fbs = self.analyst.feedback_for(alert_id)
            if not fbs:
                return None
            self._overrides[alert_id] = _VERDICT_STATUS[max(fbs, key=lambda f: f.ts).verdict]
        return self._overrides[alert_id]

    def _apply(self, a: Alert) -> Alert:
        st = self._override(a.id)
        return a if st is None or st == a.status else a.model_copy(update={"status": st})

    def upsert_alert(self, alert: Alert) -> None:
        """Only the status is retained (as an override); the pipeline owns the alert itself."""
        self._overrides[alert.id] = alert.status

    def get_alert(self, alert_id: str) -> Alert | None:
        a = self.read.get_alert(alert_id)
        return self._apply(a) if a else None

    def list_alerts(
        self,
        since: datetime | None = None,
        status: Sequence[AlertStatus] | None = None,
        aoi: str | None = None,
        limit: int = 200,
    ) -> list[Alert]:
        # status filtering must happen after the override is applied
        alerts = [self._apply(a) for a in self.read.list_alerts(since, None, aoi, limit)]
        return [a for a in alerts if status is None or a.status in status]

    # ---- analyst loop: analyst store
    def add_feedback(self, fb: Feedback) -> None:
        self.analyst.add_feedback(fb)
        self._overrides[fb.alert_id] = _VERDICT_STATUS[fb.verdict]

    def feedback_for(self, alert_id: str) -> list[Feedback]:
        return self.analyst.feedback_for(alert_id)

    def upsert_action(self, action: ActionRequest) -> None:
        self.analyst.upsert_action(action)

    def actions_for(self, alert_id: str) -> list[ActionRequest]:
        return self.analyst.actions_for(alert_id)

    def get_action(self, action_id: str) -> ActionRequest | None:
        return self.analyst.get_action(action_id)

    def close(self) -> None:
        self.read.close()
        self.analyst.close()
