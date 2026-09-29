"""DuckDB hot store implementing the Store port. Rebuildable from the lake.

Timestamps are stored as naive UTC TIMESTAMP and returned tz-aware (UTC).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel

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

T_MIN = datetime(1970, 1, 1, tzinfo=UTC)


def _to_db(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _from_db(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None else None


def _sql_type(annotation: Any) -> str:
    s = str(annotation)
    if "datetime" in s:
        return "TIMESTAMP"
    if "dict" in s or "Any" in s:
        return "VARCHAR"  # JSON text
    if "bool" in s:
        return "BOOLEAN"
    if "float" in s:
        return "DOUBLE"
    if "int" in s:
        return "BIGINT"
    return "VARCHAR"


class _Table:
    """Column layout derived from a pydantic model, so schema and insert never drift."""

    def __init__(self, name: str, model: type[BaseModel]) -> None:
        self.name = name
        self.model = model
        self.cols = list(model.model_fields)
        self.types = {c: _sql_type(f.annotation) for c, f in model.model_fields.items()}

    def ddl(self) -> str:
        cols = ", ".join(f"{c} {self.types[c]}" for c in self.cols)
        return f"CREATE TABLE IF NOT EXISTS {self.name} ({cols})"

    def insert_sql(self) -> str:
        ph = ", ".join("?" for _ in self.cols)
        return f"INSERT INTO {self.name} ({', '.join(self.cols)}) VALUES ({ph})"

    def to_row(self, m: BaseModel) -> list[Any]:
        out: list[Any] = []
        for c in self.cols:
            v = getattr(m, c)
            if self.types[c] == "TIMESTAMP":
                v = _to_db(v)
            elif self.types[c] == "VARCHAR" and isinstance(v, dict | list):
                v = json.dumps(v, default=str)
            out.append(v)
        return out

    def from_row(self, row: Sequence[Any]) -> Any:
        d: dict[str, Any] = {}
        for c, v in zip(self.cols, row, strict=True):
            if self.types[c] == "TIMESTAMP":
                v = _from_db(v)
            elif c in ("geometry", "attrs") and isinstance(v, str):
                v = json.loads(v)
            d[c] = v
        return self.model.model_validate(d)


_AIR = _Table("aircraft", AircraftObs)
_SEA = _Table("vessels", VesselObs)
_EVT = _Table("events", GeoEvent)

_DDL = [
    _AIR.ddl(),
    _SEA.ddl(),
    _EVT.ddl(),
    "CREATE INDEX IF NOT EXISTS ix_aircraft ON aircraft (h3_r4, ts)",
    "CREATE INDEX IF NOT EXISTS ix_vessels ON vessels (h3_r4, ts)",
    "CREATE INDEX IF NOT EXISTS ix_events ON events (h3_r4, ts)",
    "CREATE TABLE IF NOT EXISTS evidence (id VARCHAR PRIMARY KEY, kind VARCHAR, h3_cell VARCHAR,"
    " window_end TIMESTAMP, created_at TIMESTAMP, doc VARCHAR)",
    "CREATE INDEX IF NOT EXISTS ix_evidence ON evidence (h3_cell, kind, window_end)",
    "CREATE TABLE IF NOT EXISTS alerts (id VARCHAR PRIMARY KEY, kind VARCHAR, severity VARCHAR,"
    " status VARCHAR, aoi VARCHAR, confidence DOUBLE, first_seen TIMESTAMP, last_seen TIMESTAMP,"
    " doc VARCHAR)",
    "CREATE TABLE IF NOT EXISTS feedback (alert_id VARCHAR, verdict VARCHAR, reason VARCHAR,"
    ' "user" VARCHAR, ts TIMESTAMP)',
    "CREATE TABLE IF NOT EXISTS actions (alert_id VARCHAR, action_id VARCHAR, params VARCHAR,"
    " status VARCHAR, result VARCHAR, ts TIMESTAMP, PRIMARY KEY (alert_id, action_id))",
    "CREATE TABLE IF NOT EXISTS metrics (key VARCHAR, metric VARCHAR, ts TIMESTAMP,"
    " value DOUBLE, PRIMARY KEY (key, metric, ts))",
    "CREATE TABLE IF NOT EXISTS health (source VARCHAR PRIMARY KEY, doc VARCHAR)",
]


class DuckDBStore:
    def __init__(self, path: str | Path = ":memory:", read_only: bool = False) -> None:
        p = str(path)
        if p != ":memory:" and not read_only:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._con = duckdb.connect(p, read_only=read_only)
        self.read_only = read_only
        if not read_only:
            for stmt in _DDL:
                self._con.execute(stmt)

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return self._con.execute(sql, list(params)).fetchall()

    def _insert(self, t: _Table, rows: Sequence[BaseModel]) -> int:
        if not rows:
            return 0
        with self._lock:
            self._con.executemany(t.insert_sql(), [t.to_row(r) for r in rows])
        return len(rows)

    def _window(
        self,
        t: _Table,
        start: datetime,
        end: datetime,
        extra: str = "",
        extra_params: Sequence[Any] = (),
    ) -> list[Any]:
        sql = (
            f"SELECT {', '.join(t.cols)} FROM {t.name} WHERE ts >= ? AND ts < ? {extra} ORDER BY ts"
        )
        rows = self._exec(sql, [_to_db(start), _to_db(end), *extra_params])
        return [t.from_row(r) for r in rows]

    @staticmethod
    def _in(col: str, vals: Sequence[Any]) -> tuple[str, list[Any]]:
        return f"AND {col} IN ({', '.join('?' for _ in vals)})", list(vals)

    # ------------------------------------------------------------------ observations
    def insert_aircraft(self, rows: Sequence[AircraftObs]) -> int:
        return self._insert(_AIR, rows)

    def insert_vessels(self, rows: Sequence[VesselObs]) -> int:
        return self._insert(_SEA, rows)

    def insert_events(self, rows: Sequence[GeoEvent]) -> int:
        return self._insert(_EVT, rows)

    def aircraft_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[AircraftObs]:
        if h3_r4 is not None:
            if not h3_r4:
                return []
            sql, p = self._in("h3_r4", h3_r4)
            return self._window(_AIR, start, end, sql, p)
        return self._window(_AIR, start, end)

    def vessels_window(
        self, start: datetime, end: datetime, h3_r4: Sequence[str] | None = None
    ) -> list[VesselObs]:
        if h3_r4 is not None:
            if not h3_r4:
                return []
            sql, p = self._in("h3_r4", h3_r4)
            return self._window(_SEA, start, end, sql, p)
        return self._window(_SEA, start, end)

    def events_window(
        self, start: datetime, end: datetime, kinds: Sequence[str] | None = None
    ) -> list[GeoEvent]:
        if kinds is not None:
            if not kinds:
                return []
            sql, p = self._in("kind", kinds)
            return self._window(_EVT, start, end, sql, p)
        return self._window(_EVT, start, end)

    # ------------------------------------------------------------------ evidence & alerts
    def upsert_evidence(self, ev: Evidence) -> None:
        self._exec(
            "INSERT OR REPLACE INTO evidence VALUES (?, ?, ?, ?, ?, ?)",
            [
                ev.id,
                ev.kind.value,
                ev.h3_cell,
                _to_db(ev.window_end),
                _to_db(ev.created_at),
                ev.model_dump_json(),
            ],
        )

    def get_evidence(self, ids: Sequence[str]) -> list[Evidence]:
        if not ids:
            return []
        rows = self._exec(
            f"SELECT doc FROM evidence WHERE id IN ({', '.join('?' for _ in ids)})", list(ids)
        )
        found = {e.id: e for e in (Evidence.model_validate_json(r[0]) for r in rows)}
        return [found[i] for i in ids if i in found]

    def evidence_history(self, h3_cell: str, kind: str, since: datetime) -> list[Evidence]:
        rows = self._exec(
            "SELECT doc FROM evidence WHERE h3_cell = ? AND kind = ? AND window_end >= ?"
            " ORDER BY window_end",
            [h3_cell, kind, _to_db(since)],
        )
        return [Evidence.model_validate_json(r[0]) for r in rows]

    def upsert_alert(self, alert: Alert) -> None:
        self._exec(
            "INSERT OR REPLACE INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                alert.id,
                alert.kind.value,
                alert.severity.value,
                alert.status.value,
                alert.aoi,
                alert.confidence,
                _to_db(alert.first_seen),
                _to_db(alert.last_seen),
                alert.model_dump_json(),
            ],
        )

    def get_alert(self, alert_id: str) -> Alert | None:
        rows = self._exec("SELECT doc FROM alerts WHERE id = ?", [alert_id])
        return Alert.model_validate_json(rows[0][0]) if rows else None

    def list_alerts(
        self,
        since: datetime | None = None,
        status: Sequence[AlertStatus] | None = None,
        aoi: str | None = None,
        limit: int = 200,
    ) -> list[Alert]:
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("last_seen >= ?")
            params.append(_to_db(since))
        if status:
            where.append(f"status IN ({', '.join('?' for _ in status)})")
            params.extend(s.value for s in status)
        if aoi is not None:
            where.append("aoi = ?")
            params.append(aoi)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._exec(
            f"SELECT doc FROM alerts {clause} ORDER BY last_seen DESC LIMIT ?", [*params, limit]
        )
        return [Alert.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------ analyst loop
    def add_feedback(self, fb: Feedback) -> None:
        self._exec(
            "INSERT INTO feedback VALUES (?, ?, ?, ?, ?)",
            [fb.alert_id, fb.verdict, fb.reason, fb.user, _to_db(fb.ts)],
        )

    def feedback_for(self, alert_id: str) -> list[Feedback]:
        rows = self._exec(
            'SELECT alert_id, verdict, reason, "user", ts FROM feedback WHERE alert_id = ?'
            " ORDER BY ts",
            [alert_id],
        )
        return [
            Feedback(
                alert_id=r[0], verdict=r[1], reason=r[2], user=r[3], ts=_from_db(r[4]) or T_MIN
            )
            for r in rows
        ]

    def upsert_action(self, action: ActionRequest) -> None:
        self._exec(
            "INSERT OR REPLACE INTO actions VALUES (?, ?, ?, ?, ?, ?)",
            [
                action.alert_id,
                action.action_id,
                json.dumps(action.params, default=str),
                action.status,
                json.dumps(action.result, default=str) if action.result is not None else None,
                _to_db(action.ts),
            ],
        )

    def actions_for(self, alert_id: str) -> list[ActionRequest]:
        rows = self._exec(
            "SELECT alert_id, action_id, params, status, result, ts FROM actions"
            " WHERE alert_id = ? ORDER BY ts",
            [alert_id],
        )
        return [
            ActionRequest(
                alert_id=r[0],
                action_id=r[1],
                params=json.loads(r[2]),
                status=r[3],
                result=json.loads(r[4]) if r[4] is not None else None,
                ts=_from_db(r[5]) or T_MIN,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ baselines
    def record_metric(self, key: str, metric: str, ts: datetime, value: float) -> None:
        self._exec(
            "INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?)", [key, metric, _to_db(ts), value]
        )

    def metric_history(
        self, key: str, metric: str, since: datetime
    ) -> list[tuple[datetime, float]]:
        rows = self._exec(
            "SELECT ts, value FROM metrics WHERE key = ? AND metric = ? AND ts >= ? ORDER BY ts",
            [key, metric, _to_db(since)],
        )
        return [(_from_db(r[0]), r[1]) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ health
    def upsert_health(self, h: SourceHealth) -> None:
        self._exec("INSERT OR REPLACE INTO health VALUES (?, ?)", [h.source, h.model_dump_json()])

    def list_health(self) -> list[SourceHealth]:
        rows = self._exec("SELECT doc FROM health ORDER BY source")
        return [SourceHealth.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._con.close()
