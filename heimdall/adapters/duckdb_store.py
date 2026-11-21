"""DuckDB hot store implementing the Store port. Rebuildable from the lake.

Timestamps are stored as naive UTC TIMESTAMP and returned tz-aware (UTC).
"""

from __future__ import annotations

import contextlib
import importlib.abc
import importlib.util
import json
import sys
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from pydantic import BaseModel

from heimdall.adapters.alert_ref import format_ref, parse_ref, ref_day
from heimdall.geo import BBox, Ident
from heimdall.logging import get_logger
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


# Perf: DuckDB probes `import pandas` ~6 times per query. Without pandas installed every probe
# walks the whole import path (~1.4 s per pipeline tick measured 2026-10-03). Recording the absence
# once makes later probes fail instantly; nothing changes when pandas is installed.
class _NoPandas(importlib.abc.MetaPathFinder):
    """Answers `import pandas` with ModuleNotFoundError at once (pandas is not installed)."""

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> None:
        if name == "pandas" or name.startswith("pandas."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


if importlib.util.find_spec("pandas") is None and not any(
    isinstance(f, _NoPandas) for f in sys.meta_path
):
    sys.meta_path.insert(0, _NoPandas())


T_MIN = datetime(1970, 1, 1, tzinfo=UTC)
_log = get_logger("heimdall.duckdb")


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


_ARROW = {
    "TIMESTAMP": pa.timestamp("us"),
    "VARCHAR": pa.string(),
    "BOOLEAN": pa.bool_(),
    "DOUBLE": pa.float64(),
    "BIGINT": pa.int64(),
}


# SQL built with f-strings here (bandit B608, reviewed 066) interpolates only identifiers that are
# module constants (table/column names from the _Table schemas, fixed filter columns) and lists of
# "?" placeholders. Every value, including anything that came from a request or a feed, is bound.
class _Table:
    """Column layout derived from a pydantic model, so schema and insert never drift."""

    def __init__(self, name: str, model: type[BaseModel]) -> None:
        self.name = name
        self.model = model
        self.cols = list(model.model_fields)
        self.types = {c: _sql_type(f.annotation) for c, f in model.model_fields.items()}
        # 055: rows Heimdall validated before writing and whose fields are plain scalars
        # (aircraft, vessels: no enums or nested models) are rebuilt without re-validation:
        # ~3x faster reads and no intermediate copies at 100k+ rows per tick
        self.trusted = name in ("aircraft", "vessels")
        self._ts = {c for c in self.cols if self.types[c] == "TIMESTAMP"}
        self._fset = set(self.cols)  # shared: every column is always set on trusted rows
        # repeated short strings (hex, provider, H3 cells, ...) interned: ~25 % less memory
        self._intern = [
            c
            for c in ("hex", "provider", "src_type", "type_code", "flight", "h3_r4", "h3_r6")
            if c in self.cols
        ]

    def ddl(self) -> str:
        cols = ", ".join(f"{c} {self.types[c]}" for c in self.cols)
        return f"CREATE TABLE IF NOT EXISTS {self.name} ({cols})"

    def insert_sql(self) -> str:
        ph = ", ".join("?" for _ in self.cols)
        return f"INSERT INTO {self.name} ({', '.join(self.cols)}) VALUES ({ph})"  # nosec B608

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

    def to_arrow(self, rows: Sequence[BaseModel]) -> pa.Table:
        """Columnar batch for bulk insert (one Arrow table instead of N parameter rows)."""
        cols: dict[str, list[Any]] = {c: [] for c in self.cols}
        for m in rows:
            for c, v in zip(self.cols, self.to_row(m), strict=True):
                cols[c].append(v)
        schema = pa.schema([(c, _ARROW[self.types[c]]) for c in self.cols])
        return pa.table(cols, schema=schema)

    def from_row(self, row: Sequence[Any]) -> Any:
        if self.trusted:
            fast = dict(zip(self.cols, row, strict=True))
            for c in self._ts:
                fast[c] = _from_db(fast[c])
            if "is_global" in fast:
                fast["is_global"] = bool(fast["is_global"])  # NULL on rows written before 024g
            for c in self._intern:
                v = fast[c]
                if isinstance(v, str):
                    fast[c] = sys.intern(v)
            # what model_construct does, minus its per-field default handling (~3x faster)
            m = self.model.__new__(self.model)
            object.__setattr__(m, "__dict__", fast)
            object.__setattr__(m, "__pydantic_fields_set__", self._fset)
            object.__setattr__(m, "__pydantic_extra__", None)
            object.__setattr__(m, "__pydantic_private__", None)
            return m
        d: dict[str, Any] = {}
        for c, v in zip(self.cols, row, strict=True):
            if self.types[c] == "TIMESTAMP":
                v = _from_db(v)
            elif c == "is_global":
                v = bool(v)  # NULL on rows written before 024g
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
    "ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS is_global BOOLEAN",
    "ALTER TABLE vessels ADD COLUMN IF NOT EXISTS is_global BOOLEAN",
    # No secondary ART indexes on the observation tables: DuckDB's zonemaps already prune the
    # time-ordered scans, and deleting from a non-unique ART index (event upsert, janitor purge)
    # hit "Failed to delete all rows from index", which invalidates the whole database
    # (2026-10-01 crash). Drop them on existing files too.
    "DROP INDEX IF EXISTS ix_aircraft",
    "DROP INDEX IF EXISTS ix_vessels",
    "DROP INDEX IF EXISTS ix_events",
    "CREATE TABLE IF NOT EXISTS evidence (id VARCHAR PRIMARY KEY, kind VARCHAR, h3_cell VARCHAR,"
    " window_end TIMESTAMP, created_at TIMESTAMP, doc VARCHAR)",
    # INSERT OR REPLACE deletes from every index on the table: same bug class as above
    "DROP INDEX IF EXISTS ix_evidence",
    "CREATE TABLE IF NOT EXISTS alerts (id VARCHAR PRIMARY KEY, kind VARCHAR, severity VARCHAR,"
    " status VARCHAR, aoi VARCHAR, confidence DOUBLE, first_seen TIMESTAMP, last_seen TIMESTAMP,"
    " doc VARCHAR)",
    "CREATE TABLE IF NOT EXISTS feedback (alert_id VARCHAR, verdict VARCHAR, reason VARCHAR,"
    ' "user" VARCHAR, ts TIMESTAMP)',
    "CREATE TABLE IF NOT EXISTS actions (id VARCHAR PRIMARY KEY, alert_id VARCHAR,"
    " action_id VARCHAR, params VARCHAR, status VARCHAR, result VARCHAR, ts TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS metrics (key VARCHAR, metric VARCHAR, ts TIMESTAMP,"
    " value DOUBLE, PRIMARY KEY (key, metric, ts))",
    "CREATE TABLE IF NOT EXISTS health (source VARCHAR PRIMARY KEY, doc VARCHAR)",
    "CREATE TABLE IF NOT EXISTS notes (id VARCHAR PRIMARY KEY, alert_id VARCHAR, ts TIMESTAMP,"
    " doc VARCHAR)",
    "CREATE INDEX IF NOT EXISTS ix_notes ON notes (alert_id, ts)",
    # insertion sequence: notes are ordered by it, not by ts (API and store clocks differ)
    "CREATE SEQUENCE IF NOT EXISTS notes_seq START 1",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS seq BIGINT",
    # alert refs: stable human ref per alert id + per-UTC-day counter (survives restarts)
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS ref VARCHAR",
    "CREATE TABLE IF NOT EXISTS alert_ref_day (day VARCHAR PRIMARY KEY, n INTEGER)",
]


class DuckDBStore:
    def __init__(
        self,
        path: str | Path = ":memory:",
        read_only: bool = False,
        memory_limit: str | None = "1GB",
        threads: int | None = 4,
    ) -> None:
        p = str(path)
        if p != ":memory:" and not read_only:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        self._path = p
        self._cfg: dict[str, str] = {}
        # DuckDB defaults to 80 % of RAM and all cores; on a laptop that starves everything else
        if memory_limit:
            self._cfg["memory_limit"] = memory_limit
        if threads:
            self._cfg["threads"] = str(threads)
        self._lock = threading.RLock()
        self.read_only = read_only
        self._con = self._connect()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(self._path, read_only=self.read_only, config=dict(self._cfg))
        if not self.read_only:
            for stmt in _DDL:
                con.execute(stmt)
        return con

    def _recover(self, err: Exception) -> None:
        """A FatalException invalidates the DuckDB instance until it is reopened; reopen once so
        one bad statement cannot take the API and the writer down for the rest of the run."""
        with self._lock:
            with contextlib.suppress(Exception):
                self._con.close()
            self._con = self._connect()
        _log.error("duckdb_reopened", path=self._path, error=str(err)[:300])

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        try:
            with self._lock:
                return self._con.execute(sql, list(params)).fetchall()
        except duckdb.FatalException as e:
            self._recover(e)
            with self._lock:
                return self._con.execute(sql, list(params)).fetchall()

    def _read(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Read on a private cursor so API reads never wait for the writer lock."""
        try:
            return self._read_once(sql, params)
        except duckdb.FatalException as e:
            self._recover(e)
            return self._read_once(sql, params)

    def _read_once(self, sql: str, params: Sequence[Any]) -> list[tuple[Any, ...]]:
        cur = self._con.cursor()
        try:
            return cur.execute(sql, list(params)).fetchall()
        finally:
            cur.close()

    @contextlib.contextmanager
    def _writing(self) -> Iterator[None]:
        """Write lock; a fatal DuckDB error reopens the store before re-raising (the batch is
        lost from the hot store only; the lake already has it)."""
        with self._lock:
            try:
                yield
            except duckdb.FatalException as e:
                self._recover(e)
                raise

    @staticmethod
    def _count(res: list[tuple[Any, ...]]) -> int:
        """Affected-row count of a DuckDB DELETE (one row, one column)."""
        return int(res[0][0]) if res and res[0] and res[0][0] is not None else 0

    def _insert(self, t: _Table, rows: Sequence[BaseModel]) -> int:
        if not rows:
            return 0
        self._bulk(t, rows)
        return len(rows)

    def _bulk(self, t: _Table, rows: Sequence[BaseModel]) -> None:
        """INSERT ... SELECT from a registered Arrow table (026a: ~50x faster than executemany).

        Called with the write lock held or not: the Arrow build happens outside the lock.
        """
        tbl = t.to_arrow(rows)
        with self._writing():
            name = f"_batch_{t.name}"
            self._con.register(name, tbl)
            try:
                cols = ", ".join(t.cols)
                self._con.execute(f"INSERT INTO {t.name} ({cols}) SELECT {cols} FROM {name}")  # nosec B608
            finally:
                self._con.unregister(name)

    def _window(
        self,
        t: _Table,
        start: datetime,
        end: datetime,
        extra: str = "",
        extra_params: Sequence[Any] = (),
    ) -> list[Any]:
        sql = (
            f"SELECT {', '.join(t.cols)} FROM {t.name} WHERE ts >= ? AND ts < ? {extra} ORDER BY ts"  # nosec B608
        )
        return self._read_models(t, sql, [_to_db(start), _to_db(end), *extra_params])

    def _read_models(self, t: _Table, sql: str, params: Sequence[Any]) -> list[Any]:
        """055: stream in batches so raw rows and models never all sit in memory together."""
        try:
            return self._read_models_once(t, sql, params)
        except duckdb.FatalException as e:
            self._recover(e)
            return self._read_models_once(t, sql, params)

    def _read_models_once(self, t: _Table, sql: str, params: Sequence[Any]) -> list[Any]:
        cur = self._con.cursor()
        out: list[Any] = []
        try:
            cur.execute(sql, list(params))
            while batch := cur.fetchmany(10_000):
                out.extend(t.from_row(r) for r in batch)
        finally:
            cur.close()
        return out

    def _filters(
        self,
        h3_r4: Sequence[str] | None,
        bbox: BBox | None,
        ident: Ident | None,
        key: str,
    ) -> tuple[str | None, list[Any]]:
        """AND-clauses for the optional window filters; sql None = provably empty."""
        sql: str = ""
        p: list[Any] = []
        if h3_r4 is not None:
            if not h3_r4:
                return None, []
            s1, p1 = self._in("h3_r4", h3_r4)
            sql, p = sql + " " + s1, p + p1
        if ident is not None:
            if isinstance(ident, frozenset):
                if not ident:
                    return None, []
                s2, p2 = self._in(key, sorted(ident))
                sql, p = sql + " " + s2, [*p, *p2]
            else:
                sql, p = sql + f" AND {key} = ?", [*p, ident]
        if bbox is not None:
            w, s, e, n = bbox
            lon = "lon >= ? AND lon <= ?" if w <= e else "(lon >= ? OR lon <= ?)"
            sql, p = sql + f" AND lat >= ? AND lat <= ? AND {lon}", [*p, s, n, w, e]
        return sql, p

    @staticmethod
    def _in(col: str, vals: Sequence[Any]) -> tuple[str, list[Any]]:
        return f"AND {col} IN ({', '.join('?' for _ in vals)})", list(vals)

    # ------------------------------------------------------------------ observations
    def insert_aircraft(self, rows: Sequence[AircraftObs]) -> int:
        return self._insert(_AIR, rows)

    def insert_vessels(self, rows: Sequence[VesselObs]) -> int:
        return self._insert(_SEA, rows)

    def purge_global(self, before: datetime) -> int:
        """Delete display-only global rows older than `before` (024g janitor)."""
        n = 0
        with self._writing():
            for t in ("aircraft", "vessels"):
                # count only: `RETURNING 1` materialised millions of rows in Python
                r = self._con.execute(
                    f"DELETE FROM {t} WHERE is_global AND ts < ?",  # nosec B608
                    [_to_db(before)],
                ).fetchall()
                n += self._count(r)
        return n

    def purge_hot(self, obs_before: datetime, events_before: datetime | None = None) -> int:
        """Retention: drop aircraft/vessel rows older than `obs_before` and events older
        than `events_before`. The lake keeps everything; this only bounds the hot store."""
        n = 0
        with self._writing():
            for t, cut in (
                ("aircraft", obs_before),
                ("vessels", obs_before),
                ("events", events_before),
            ):
                if cut is None:
                    continue
                r = self._con.execute(f"DELETE FROM {t} WHERE ts < ?", [_to_db(cut)]).fetchall()  # nosec B608
                n += self._count(r)
        return n

    def insert_events(self, rows: Sequence[GeoEvent]) -> int:
        if not rows:
            return 0
        latest = {r.id: r for r in rows}  # last write wins, also within the batch
        tbl = _EVT.to_arrow(list(latest.values()))
        with self._writing():  # delete-by-id + insert atomically; readers see either state
            self._con.execute("BEGIN")
            try:
                self._con.register("_batch_events", tbl)
                # one set-based delete instead of one statement per id
                self._con.execute("DELETE FROM events WHERE id IN (SELECT id FROM _batch_events)")
                cols = ", ".join(_EVT.cols)
                self._con.execute(f"INSERT INTO events ({cols}) SELECT {cols} FROM _batch_events")  # nosec B608
                self._con.unregister("_batch_events")
                self._con.execute("COMMIT")
            except duckdb.FatalException:
                raise  # the instance is invalid; ROLLBACK would fail too
            except Exception:
                with contextlib.suppress(Exception):
                    self._con.unregister("_batch_events")
                self._con.execute("ROLLBACK")
                raise
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
        mil_types: Sequence[str] = (),
    ) -> list[AircraftObs]:
        sql, p = self._filters(h3_r4, bbox, ident, "hex")
        if sql is None:
            return []
        if local_only:  # 024g display-only rows never feed detectors; skip them in SQL
            sql += " AND NOT coalesce(is_global, false)"
        if mil_only:  # military rows only (any source); 055: or a military type code
            if mil_types:
                ph = ", ".join("?" for _ in mil_types)
                sql += f" AND (coalesce(mil, false) OR upper(type_code) IN ({ph}))"
                p = [*p, *(t.upper() for t in mil_types)]
            else:
                sql += " AND coalesce(mil, false)"
        return self._window(_AIR, start, end, sql, p)

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
        sql, p = self._filters(h3_r4, bbox, ident, "mmsi")
        if sql is None:
            return []
        if local_only:  # 024g display-only rows never feed detectors; skip them in SQL
            sql += " AND NOT coalesce(is_global, false)"
        return self._window(_SEA, start, end, sql, p)

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
            f"SELECT doc FROM evidence WHERE id IN ({', '.join('?' for _ in ids)})",  # nosec B608
            list(ids),
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

    def _ref_for(self, alert: Alert) -> str:
        """Keep the stored ref; else honour a well-formed incoming one (lake rebuild,
        bumping the day counter past it); else take the next per-UTC-day number."""
        with self._lock:
            rows = self._con.execute("SELECT ref FROM alerts WHERE id = ?", [alert.id]).fetchall()
            if rows and rows[0][0]:
                return str(rows[0][0])
            got = parse_ref(alert.ref)
            if got is not None:
                day, n = got
                self._con.execute(
                    "INSERT INTO alert_ref_day VALUES (?, ?) ON CONFLICT (day) DO UPDATE"
                    " SET n = greatest(alert_ref_day.n, excluded.n)",
                    [day, n],
                )
                return str(alert.ref)
            day = ref_day(alert.first_seen)
            n = int(
                self._con.execute(
                    "INSERT INTO alert_ref_day VALUES (?, 1) ON CONFLICT (day) DO UPDATE"
                    " SET n = alert_ref_day.n + 1 RETURNING n",
                    [day],
                ).fetchall()[0][0]
            )
            return format_ref(day, n)

    def upsert_alert(self, alert: Alert) -> None:
        with self._writing():
            alert = alert.model_copy(update={"ref": self._ref_for(alert)})
            self._con.execute(
                "INSERT OR REPLACE INTO alerts (id, kind, severity, status, aoi, confidence,"
                " first_seen, last_seen, doc, ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    alert.ref,
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
        rows = self._read(  # API/SSE poll: private cursor, never queued behind the writer
            f"SELECT doc FROM alerts {clause} ORDER BY last_seen DESC LIMIT ?",  # nosec B608
            [*params, limit],
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

    def add_note(self, note: AnalystNote) -> None:
        """Insert with the next insertion sequence; re-adding an id keeps its original slot."""
        with self._lock:
            if self._exec("SELECT 1 FROM notes WHERE id = ?", [note.id]):
                self._exec(
                    "UPDATE notes SET alert_id = ?, ts = ?, doc = ? WHERE id = ?",
                    [note.alert_id, _to_db(note.ts), note.model_dump_json(), note.id],
                )
                return
            self._exec(
                "INSERT INTO notes (id, alert_id, ts, doc, seq)"
                " VALUES (?, ?, ?, ?, nextval('notes_seq'))",
                [note.id, note.alert_id, _to_db(note.ts), note.model_dump_json()],
            )

    def notes_for(self, alert_id: str) -> list[AnalystNote]:
        # legacy rows (seq NULL, written before the column existed) first, by ts
        rows = self._exec(
            "SELECT doc FROM notes WHERE alert_id = ? ORDER BY seq NULLS FIRST, ts", [alert_id]
        )
        return [AnalystNote.model_validate_json(r[0]) for r in rows]

    def record_status_change(self, note: AnalystNote) -> None:
        with self._lock:
            self.add_note(note)
            cur = self.get_alert(note.alert_id)
            if cur is not None and note.status_to is not None:
                self.upsert_alert(cur.model_copy(update={"status": note.status_to}))

    def upsert_action(self, action: ActionRequest) -> None:
        self._exec(
            "INSERT OR REPLACE INTO actions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                action.id,
                action.alert_id,
                action.action_id,
                json.dumps(action.params, default=str),
                action.status,
                json.dumps(action.result, default=str) if action.result is not None else None,
                _to_db(action.ts),
            ],
        )

    _ACTION_COLS = "id, alert_id, action_id, params, status, result, ts"

    @staticmethod
    def _action(r: Sequence[Any]) -> ActionRequest:
        return ActionRequest(
            id=r[0],
            alert_id=r[1],
            action_id=r[2],
            params=json.loads(r[3]),
            status=r[4],
            result=json.loads(r[5]) if r[5] is not None else None,
            ts=_from_db(r[6]) or T_MIN,
        )

    def actions_for(self, alert_id: str) -> list[ActionRequest]:
        rows = self._exec(
            f"SELECT {self._ACTION_COLS} FROM actions WHERE alert_id = ? ORDER BY ts, id",  # nosec B608
            [alert_id],
        )
        return [self._action(r) for r in rows]

    def get_action(self, action_id: str) -> ActionRequest | None:
        rows = self._exec(
            f"SELECT {self._ACTION_COLS} FROM actions WHERE id = ?",  # nosec B608
            [action_id],
        )
        return self._action(rows[0]) if rows else None

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
