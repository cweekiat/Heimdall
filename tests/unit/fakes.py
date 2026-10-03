"""In-memory fake Store (dict-backed) for pipeline tests. Implements the Store port."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from heimdall.geo import BBox, Ident, obs_match
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


class FakeStore:
    def __init__(self) -> None:
        self.aircraft: list[AircraftObs] = []
        self.vessels: list[VesselObs] = []
        self.events: list[GeoEvent] = []
        self.evidence: dict[str, Evidence] = {}
        self.alerts: dict[str, Alert] = {}
        self.feedback: list[Feedback] = []
        self.actions: dict[str, ActionRequest] = {}
        self.notes: list[AnalystNote] = []
        # PK (key, metric, ts) like DuckDBStore: record_metric is an upsert
        self.metrics: dict[tuple[str, str], dict[datetime, float]] = defaultdict(dict)
        self.health: dict[str, SourceHealth] = {}

    def insert_aircraft(self, rows: Sequence[AircraftObs]) -> int:
        self.aircraft.extend(rows)
        return len(rows)

    def insert_vessels(self, rows: Sequence[VesselObs]) -> int:
        self.vessels.extend(rows)
        return len(rows)

    def insert_events(self, rows: Sequence[GeoEvent]) -> int:
        """Idempotent on GeoEvent.id, last write wins (ADR-004 section 4)."""
        by_id = {e.id: i for i, e in enumerate(self.events)}
        for r in rows:
            if r.id in by_id:
                self.events[by_id[r.id]] = r
            else:
                by_id[r.id] = len(self.events)
                self.events.append(r)
        return len(rows)

    def aircraft_window(
        self,
        start: datetime,
        end: datetime,
        h3_r4: Sequence[str] | None = None,
        *,
        bbox: BBox | None = None,
        ident: Ident | None = None,
        local_only: bool = False,
        mil_only: bool = False,
        mil_types: Sequence[str] = (),
    ) -> list[AircraftObs]:
        return [
            o
            for o in self.aircraft
            if start <= o.ts < end
            and (h3_r4 is None or o.h3_r4 in h3_r4)
            and obs_match(o, bbox, ident, "hex")
            and not (local_only and o.is_global)
            and (o.mil or not mil_only or (o.type_code or "").upper() in mil_types)
        ]

    def vessels_window(
        self,
        start: datetime,
        end: datetime,
        h3_r4: Sequence[str] | None = None,
        *,
        bbox: BBox | None = None,
        ident: Ident | None = None,
        local_only: bool = False,
    ) -> list[VesselObs]:
        return [
            v
            for v in self.vessels
            if start <= v.ts < end
            and obs_match(v, bbox, ident, "mmsi")
            and not (local_only and v.is_global)
        ]

    def events_window(
        self, start: datetime, end: datetime, kinds: Sequence[str] | None = None
    ) -> list[GeoEvent]:
        return [
            e for e in self.events if start <= e.ts < end and (kinds is None or e.kind in kinds)
        ]

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

    def add_note(self, note: AnalystNote) -> None:
        self.notes.append(note)

    def notes_for(self, alert_id: str) -> list[AnalystNote]:
        return [n for n in self.notes if n.alert_id == alert_id]

    def record_status_change(self, note: AnalystNote) -> None:
        self.add_note(note)
        cur = self.alerts.get(note.alert_id)
        if cur is not None and note.status_to is not None:
            self.alerts[cur.id] = cur.model_copy(update={"status": note.status_to})

    def upsert_action(self, action: ActionRequest) -> None:
        self.actions[action.id] = action

    def actions_for(self, alert_id: str) -> list[ActionRequest]:
        return [a for a in self.actions.values() if a.alert_id == alert_id]

    def get_action(self, action_id: str) -> ActionRequest | None:
        return self.actions.get(action_id)

    def record_metric(self, key: str, metric: str, ts: datetime, value: float) -> None:
        self.metrics[(key, metric)][ts] = value

    def metric_history(
        self, key: str, metric: str, since: datetime
    ) -> list[tuple[datetime, float]]:
        series = self.metrics.get((key, metric), {})
        return sorted((t, v) for t, v in series.items() if t >= since)

    def upsert_health(self, h: SourceHealth) -> None:
        self.health[h.source] = h

    def list_health(self) -> list[SourceHealth]:
        return list(self.health.values())

    def close(self) -> None:
        pass
