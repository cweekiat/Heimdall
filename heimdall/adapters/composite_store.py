"""Store that reads from a (read-only) main store and keeps analyst writes in a separate store.

DuckDB is single-writer and the ingest process owns the main file, so the API must never write
there. Analyst state (feedback, actions) lives in `analyst`. Alerts are never copied: only a
status override (alert_id -> status) is kept and applied over the pipeline's current Alert, so
pipeline updates (last_seen, confidence, evidence) always show through and pipeline upserts never
overwrite analyst status. Overrides survive restarts: they are re-derived from the latest journal
status note, falling back to the latest analyst feedback verdict when no status note exists.
Shelving expires (ADR-003): the alert returns to acknowledged and a system note records it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from heimdall.ports import Store
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


class CompositeStore:
    def __init__(
        self, read: Store, analyst: Store, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.read = read
        self.analyst = analyst
        self._now: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._overrides: dict[str, AlertStatus] = {}
        self._no_override: set[str] = set()  # cached "no notes/feedback" to avoid N+1 lookups
        self._shelved_until: dict[str, datetime] = {}

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
        """Latest analyst status for the alert; an expired shelve returns to acknowledged."""
        if alert_id in self._no_override:
            return None
        if alert_id not in self._overrides:
            latest = [n for n in self.analyst.notes_for(alert_id) if n.status_to is not None]
            if latest:
                # insertion order, not ts: the API and this store may run on different clocks
                last = latest[-1]
                self._overrides[alert_id] = last.status_to  # type: ignore[assignment]
                if last.status_to == AlertStatus.SHELVED and last.shelve_until:
                    self._shelved_until[alert_id] = last.shelve_until
            else:
                fbs = self.analyst.feedback_for(alert_id)
                if not fbs:
                    self._no_override.add(alert_id)
                    return None
                verdict = max(fbs, key=lambda f: f.ts).verdict
                self._overrides[alert_id] = (
                    AlertStatus.CONFIRMED if verdict == "confirm" else AlertStatus.DISMISSED
                )
        st = self._overrides[alert_id]
        until = self._shelved_until.get(alert_id)
        now = self._now()
        if st == AlertStatus.SHELVED and until is not None and until <= now:
            # ADR-003: shelved returns to acknowledged on expiry; journal it so there is no gap
            self.add_note(
                AnalystNote(
                    id=uuid.uuid4().hex,
                    alert_id=alert_id,
                    ts=now,
                    kind="status",
                    user="system",
                    text="shelve expired; returned to acknowledged",
                    status_to=AlertStatus.ACKNOWLEDGED,
                )
            )
            return AlertStatus.ACKNOWLEDGED
        return st

    def _apply(self, a: Alert) -> Alert:
        st = self._override(a.id)
        return a if st is None or st == a.status else a.model_copy(update={"status": st})

    def upsert_alert(self, alert: Alert) -> None:
        """Pipeline write. Goes to the main store; analyst status/shelve state is untouched."""
        self.read.upsert_alert(alert)

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
        # invalidate so the next read re-derives (notes still take precedence over feedback)
        self._no_override.discard(fb.alert_id)
        self._overrides.pop(fb.alert_id, None)

    def add_note(self, note: AnalystNote) -> None:
        self.analyst.add_note(note)
        if note.status_to is not None:
            self._no_override.discard(note.alert_id)
            self._overrides[note.alert_id] = note.status_to
            if note.shelve_until:
                self._shelved_until[note.alert_id] = note.shelve_until
            else:
                self._shelved_until.pop(note.alert_id, None)

    def notes_for(self, alert_id: str) -> list[AnalystNote]:
        return self.analyst.notes_for(alert_id)

    def record_status_change(self, note: AnalystNote) -> None:
        """Status is derived from the journal: adding the note is the status change."""
        self.add_note(note)

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
