"""In-memory fake Store (dict-backed) for pipeline tests. Implements the Store port."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

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


class FakeStore:
    def __init__(self) -> None:
        self.aircraft: list[AircraftObs] = []
        self.vessels: list[VesselObs] = []
        self.events: list[GeoEvent] = []
        self.evidence: dict[str, Evidence] = {}
        self.alerts: dict[str, Alert] = {}
        self.feedback: list[Feedback] = []
        self.actions: dict[tuple[str, str], ActionRequest] = {}
        self.metrics: dict[tuple[str, str], list[tuple[datetime, float]]] = defaultdict(list)
        self.health: dict[str, SourceHealth] = {}

    def insert_aircraft(self, rows: Sequence[AircraftObs]) -> int:
        self.aircraft.extend(rows)
        return len(rows)

    def insert_vessels(self, rows: Sequence[VesselObs]) -> int:
        self.vessels.extend(rows)
        return len(rows)

    def insert_events(self, rows: Sequence[GeoEvent]) -> int:
        self.events.extend(rows)
        return len(rows)

    def aircraft_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[AircraftObs]:
        return [
            o for o in self.aircraft if start <= o.ts < end and (h3_r4 is None or o.h3_r4 in h3_r4)
        ]

    def vessels_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[VesselObs]:
        return [v for v in self.vessels if start <= v.ts < end]

    def events_window(
        self, start: datetime, end: datetime, kinds: Sequence[str] | None = None
    ) -> list[GeoEvent]:
        return [e for e in self.events if start <= e.ts < end]

    def upsert_evidence(self, ev: Evidence) -> None:
        self.evidence[ev.id] = ev

    def get_evidence(self, ids: Sequence[str]) -> list[Evidence]:
        return [self.evidence[i] for i in ids if i in self.evidence]

    def evidence_history(self, h3_cell: str, kind: str, since: datetime) -> list[Evidence]:
        return sorted(
            (
                e
                for e in self.evidence.values()
                if e.h3_cell == h3_cell and str(e.kind) == kind and e.window_end >= since
            ),
            key=lambda e: e.window_end,
        )

    def upsert_alert(self, alert: Alert) -> None:
        self.alerts[alert.id] = alert

    def get_alert(self, alert_id: str) -> Alert | None:
        return self.alerts.get(alert_id)

    def list_alerts(
        self,
        since: datetime | None = None,
        status: Sequence[AlertStatus] | None = None,
        aoi: str | None = None,
        limit: int = 200,
    ) -> list[Alert]:
        out = [
            a
            for a in self.alerts.values()
            if (since is None or a.last_seen >= since)
            and (status is None or a.status in status)
            and (aoi is None or a.aoi == aoi)
        ]
        return sorted(out, key=lambda a: a.last_seen, reverse=True)[:limit]

    def add_feedback(self, fb: Feedback) -> None:
        self.feedback.append(fb)

    def feedback_for(self, alert_id: str) -> list[Feedback]:
        return [f for f in self.feedback if f.alert_id == alert_id]

    def upsert_action(self, action: ActionRequest) -> None:
        self.actions[(action.alert_id, action.action_id)] = action

    def actions_for(self, alert_id: str) -> list[ActionRequest]:
        return [a for (aid, _), a in self.actions.items() if aid == alert_id]

    def record_metric(self, key: str, metric: str, ts: datetime, value: float) -> None:
        self.metrics[(key, metric)].append((ts, value))

    def metric_history(
        self, key: str, metric: str, since: datetime
    ) -> list[tuple[datetime, float]]:
        return [(t, v) for t, v in self.metrics.get((key, metric), []) if t >= since]

    def upsert_health(self, h: SourceHealth) -> None:
        self.health[h.source] = h

    def list_health(self) -> list[SourceHealth]:
        return list(self.health.values())

    def close(self) -> None:
        pass
