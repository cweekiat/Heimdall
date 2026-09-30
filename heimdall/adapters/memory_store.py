"""Dict-backed Store implementation for tests and the demo server."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from heimdall.schemas import (
    ActionRequest,
    AircraftObs,
    Alert,
    AlertStatus,
    AnalystNote,
    Evidence,
    Feedback,
    GeoEvent,
    SourceHealth,
    VesselObs,
)


class MemoryStore:
    def __init__(self) -> None:
        self.aircraft: list[AircraftObs] = []
        self.vessels: list[VesselObs] = []
        self.events: list[GeoEvent] = []
        self.evidence: dict[str, Evidence] = {}
        self.alerts: dict[str, Alert] = {}
        self.feedback: list[Feedback] = []
        self.actions: list[ActionRequest] = []
        self.notes: list[AnalystNote] = []
        self.metrics: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
        self.health: dict[str, SourceHealth] = {}

    # observations
    def insert_aircraft(self, rows: Sequence[AircraftObs]) -> int:
        self.aircraft.extend(rows)
        return len(rows)

    def insert_vessels(self, rows: Sequence[VesselObs]) -> int:
        self.vessels.extend(rows)
        return len(rows)

    def insert_events(self, rows: Sequence[GeoEvent]) -> int:
        latest = {r.id: r for r in rows}  # last write wins (ADR-004)
        self.events = [e for e in self.events if e.id not in latest]
        self.events.extend(latest.values())
        return len(latest)

    def aircraft_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[AircraftObs]:
        return [
            r for r in self.aircraft if start <= r.ts < end and (h3_r4 is None or r.h3_r4 in h3_r4)
        ]

    def vessels_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[VesselObs]:
        return [
            r for r in self.vessels if start <= r.ts < end and (h3_r4 is None or r.h3_r4 in h3_r4)
        ]

    def events_window(
        self, start: datetime, end: datetime, kinds: Sequence[str] | None = None
    ) -> list[GeoEvent]:
        return [
            r for r in self.events if start <= r.ts < end and (kinds is None or r.kind in kinds)
        ]

    # evidence & alerts
    def upsert_evidence(self, ev: Evidence) -> None:
        self.evidence[ev.id] = ev

    def get_evidence(self, ids: Sequence[str]) -> list[Evidence]:
        return [self.evidence[i] for i in ids if i in self.evidence]

    def evidence_history(self, h3_cell: str, kind: str, since: datetime) -> list[Evidence]:
        return sorted(
            (
                e
                for e in self.evidence.values()
                if e.h3_cell == h3_cell and e.kind.value == kind and e.window_end >= since
            ),
            key=lambda e: e.window_end,
        )

    def upsert_alert(self, alert: Alert) -> None:
        """Pipeline upserts (status NEW) never overwrite an analyst-set status (ADR-003)."""
        cur = self.alerts.get(alert.id)
        if (
            cur is not None
            and alert.status == AlertStatus.NEW
            and cur.status not in (AlertStatus.NEW, AlertStatus.EXPIRED)
        ):
            alert = alert.model_copy(update={"status": cur.status})
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
        out.sort(key=lambda a: a.last_seen, reverse=True)
        return out[:limit]

    # analyst loop
    def add_feedback(self, fb: Feedback) -> None:
        self.feedback.append(fb)

    def feedback_for(self, alert_id: str) -> list[Feedback]:
        return [f for f in self.feedback if f.alert_id == alert_id]

    def upsert_action(self, action: ActionRequest) -> None:
        self.actions = [a for a in self.actions if a.id != action.id]
        self.actions.append(action)

    def actions_for(self, alert_id: str) -> list[ActionRequest]:
        return [a for a in self.actions if a.alert_id == alert_id]

    def add_note(self, note: AnalystNote) -> None:
        self.notes = [n for n in self.notes if n.id != note.id] + [note]

    def notes_for(self, alert_id: str) -> list[AnalystNote]:
        return sorted((n for n in self.notes if n.alert_id == alert_id), key=lambda n: n.ts)

    def get_action(self, action_id: str) -> ActionRequest | None:
        return next((a for a in self.actions if a.id == action_id), None)

    # baselines
    def record_metric(self, key: str, metric: str, ts: datetime, value: float) -> None:
        self.metrics.setdefault((key, metric), []).append((ts, value))

    def metric_history(
        self, key: str, metric: str, since: datetime
    ) -> list[tuple[datetime, float]]:
        return sorted(p for p in self.metrics.get((key, metric), []) if p[0] >= since)

    # health
    def upsert_health(self, h: SourceHealth) -> None:
        self.health[h.source] = h

    def list_health(self) -> list[SourceHealth]:
        return sorted(self.health.values(), key=lambda h: h.source)

    def close(self) -> None:
        return None
