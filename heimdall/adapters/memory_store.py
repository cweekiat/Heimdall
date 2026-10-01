"""Dict-backed Store implementation for tests and the demo server."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from heimdall.adapters.alert_ref import format_ref, parse_ref, ref_day
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


class MemoryStore:
    def __init__(self) -> None:
        self.aircraft: list[AircraftObs] = []
        self.vessels: list[VesselObs] = []
        self.events: list[GeoEvent] = []
        self.evidence: dict[str, Evidence] = {}
        self.alerts: dict[str, Alert] = {}
        self._ref_counter: dict[str, int] = {}  # UTC day (YYYYMMDD) -> last number issued
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

    def purge_global(self, before: datetime) -> int:
        n = len(self.aircraft) + len(self.vessels)
        self.aircraft = [r for r in self.aircraft if not (r.is_global and r.ts < before)]
        self.vessels = [r for r in self.vessels if not (r.is_global and r.ts < before)]
        return n - len(self.aircraft) - len(self.vessels)

    def insert_events(self, rows: Sequence[GeoEvent]) -> int:
        latest = {r.id: r for r in rows}  # last write wins (ADR-004)
        self.events = [e for e in self.events if e.id not in latest]
        self.events.extend(latest.values())
        return len(latest)

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
    ) -> list[AircraftObs]:
        return [
            r
            for r in self.aircraft
            if start <= r.ts < end
            and (h3_r4 is None or r.h3_r4 in h3_r4)
            and obs_match(r, bbox, ident, "hex")
            and not (local_only and r.is_global)
            and (r.mil or not mil_only)
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
            r
            for r in self.vessels
            if start <= r.ts < end
            and (h3_r4 is None or r.h3_r4 in h3_r4)
            and obs_match(r, bbox, ident, "mmsi")
            and not (local_only and r.is_global)
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
        self.alerts[alert.id] = alert.model_copy(update={"ref": self._ref_for(alert, cur)})

    def _ref_for(self, alert: Alert, cur: Alert | None) -> str:
        """ADR-006: keep the stored ref; else honour a well-formed incoming one (lake rebuild,
        bumping the day counter past it); else take the next per-UTC-day number."""
        if cur is not None and cur.ref:
            return cur.ref
        got = parse_ref(alert.ref)
        if got is not None:
            day, n = got
            self._ref_counter[day] = max(self._ref_counter.get(day, 0), n)
            return str(alert.ref)
        day = ref_day(alert.first_seen)
        self._ref_counter[day] = self._ref_counter.get(day, 0) + 1
        return format_ref(day, self._ref_counter[day])

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
        """Append; re-adding an existing id updates it in place (keeps its insertion slot)."""
        for i, n in enumerate(self.notes):
            if n.id == note.id:
                self.notes[i] = note
                return
        self.notes.append(note)

    def notes_for(self, alert_id: str) -> list[AnalystNote]:
        return [n for n in self.notes if n.alert_id == alert_id]  # insertion order

    def record_status_change(self, note: AnalystNote) -> None:
        self.add_note(note)
        cur = self.alerts.get(note.alert_id)
        if cur is not None and note.status_to is not None:
            self.alerts[cur.id] = cur.model_copy(update={"status": note.status_to})

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
