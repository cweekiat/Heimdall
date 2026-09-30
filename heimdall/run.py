"""Live runtime: connectors -> bus 'obs.air' -> writer (Lake + Store) -> periodic detection tick."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import signal
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel

from heimdall.adapters.composite_store import CompositeStore
from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_bus import MemoryBus
from heimdall.adapters.parquet_lake import ParquetLake
from heimdall.config import Settings
from heimdall.config import settings as load_settings
from heimdall.geo import haversine_km
from heimdall.ingest.adsb import merge_providers
from heimdall.ingest.registry import build_connectors
from heimdall.logging import configure_logging, get_logger, start_metrics_server
from heimdall.ports import Bus, Connector, Lake, Store
from heimdall.schemas import AircraftObs, GeoEvent, VesselObs

log = get_logger("heimdall.run")

TOPIC_AIR = "obs.air"
TOPIC_SEA = "obs.sea"
TOPIC_EVENTS = "events"
FLUSH_S = 5.0
HEALTH_S = 60.0


def route(batch: Sequence[BaseModel]) -> dict[str, list[BaseModel]]:
    """Split a connector batch by row type (ADR-004 item 6); unknown row types are skipped."""
    out: dict[str, list[BaseModel]] = {}
    for row in batch:
        if isinstance(row, AircraftObs):
            out.setdefault(TOPIC_AIR, []).append(row)
        elif isinstance(row, VesselObs):
            out.setdefault(TOPIC_SEA, []).append(row)
        elif isinstance(row, GeoEvent):
            out.setdefault(TOPIC_EVENTS, []).append(row)
        else:
            log.warning("unroutable_row", row_type=type(row).__name__)
    return out


async def pump(conn: Connector, bus: Bus) -> None:
    try:
        async for batch in conn.stream():
            for topic, rows in route(batch).items():
                await bus.publish(topic, rows)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # one broken source must not stop the others
        log.error("connector_crashed", source=conn.name, error=f"{type(e).__name__}: {e}")


DEDUPE_TTL_S = 30.0
DEDUPE_DT_S = 2.0
DEDUPE_KM = 0.5


class _Recent:
    __slots__ = ("kept", "last_seen", "providers")

    def __init__(self) -> None:
        self.kept: list[tuple[datetime, float, float, datetime]] = []  # ts, lat, lon, inserted
        self.providers: set[str] = set()
        self.last_seen: datetime = datetime.min.replace(tzinfo=UTC)


class Writer:
    """Buffers bus batches, de-duplicates across providers and flushes, writes Lake then Store.

    A rolling per-hex record of recently kept fixes (TTL `ttl_s`) catches duplicates that
    arrive in different flushes; the same TTL bounds the providers-seen side table.
    """

    def __init__(self, bus: Bus, lake: Lake, store: Store, ttl_s: float = DEDUPE_TTL_S) -> None:
        self.bus, self.lake, self.store = bus, lake, store
        self.ttl = timedelta(seconds=ttl_s)
        self.buf: list[AircraftObs] = []
        self.sea_buf: list[VesselObs] = []
        self.evt_buf: list[GeoEvent] = []
        self._sea_seen: dict[tuple[int, datetime], datetime] = {}  # (mmsi, ts) -> inserted (wall)
        self.n_sea = 0
        self.n_events = 0
        self._recent: dict[str, _Recent] = {}
        self.n_written = 0

    @property
    def providers(self) -> dict[str, set[str]]:
        return {h: set(r.providers) for h, r in self._recent.items()}

    async def consume(self) -> None:
        async def _one(topic: str, buf: list[Any], typ: type[BaseModel]) -> None:
            async for batch in self.bus.subscribe(topic):
                buf.extend(b for b in batch if isinstance(b, typ))

        await asyncio.gather(
            _one(TOPIC_AIR, self.buf, AircraftObs),
            _one(TOPIC_SEA, self.sea_buf, VesselObs),
            _one(TOPIC_EVENTS, self.evt_buf, GeoEvent),
        )

    def _is_dup(self, o: AircraftObs) -> bool:
        r = self._recent.get(o.hex)
        return r is not None and any(
            abs((o.ts - ts).total_seconds()) < DEDUPE_DT_S
            and haversine_km(o.lat, o.lon, lat, lon) < DEDUPE_KM
            for ts, lat, lon, _ in r.kept
        )

    def flush(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        rows, self.buf = self.buf, []
        kept, prov = merge_providers(rows)
        for hex_, p in prov.items():
            r = self._recent.setdefault(hex_, _Recent())
            r.providers.update(p)
            r.last_seen = now
        fresh = [o for o in kept if not self._is_dup(o)]
        for o in fresh:
            self._recent[o.hex].kept.append((o.ts, o.lat, o.lon, now))
        # expire old state
        for hex_ in [h for h, r in self._recent.items() if now - r.last_seen > self.ttl]:
            del self._recent[hex_]
        for r in self._recent.values():
            r.kept = [k for k in r.kept if now - k[3] <= self.ttl]  # wall time, not obs time
        self._flush_sea(now)
        self._flush_events()
        if not fresh:
            return 0
        self.lake.append("aircraft", fresh)
        n = self.store.insert_aircraft(fresh)
        self.n_written += n
        return n

    def _flush_sea(self, now: datetime) -> None:
        rows, self.sea_buf = self.sea_buf, []
        for k in [k for k, t in self._sea_seen.items() if now - t > self.ttl]:
            del self._sea_seen[k]
        fresh: list[VesselObs] = []
        for v in rows:
            key = (v.mmsi, v.ts)
            if key not in self._sea_seen:
                self._sea_seen[key] = now
                fresh.append(v)
        if fresh:
            self.lake.append("vessels", fresh)
            self.n_sea += self.store.insert_vessels(fresh)

    def _flush_events(self) -> None:
        rows, self.evt_buf = self.evt_buf, []
        if rows:  # Lake keeps every revision; the Store upserts by id (ADR-004)
            self.lake.append("events", rows)
            self.n_events += self.store.insert_events(rows)

    async def flush_loop(self, interval: float = FLUSH_S) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await asyncio.to_thread(self.flush)
        finally:
            self.flush()


async def health_loop(conns: Sequence[Connector], store: Store) -> None:
    while True:
        for c in conns:
            store.upsert_health(c.health())
        await asyncio.sleep(HEALTH_S)


async def tick_loop(store: Store, cfg: Settings, step_min: int) -> None:
    while True:
        await asyncio.sleep(step_min * 60)
        try:
            pipeline = importlib.import_module("heimdall.pipeline")
        except ImportError:
            log.info("pipeline_unavailable", note="heimdall.pipeline not installed; skipping tick")
            continue
        try:
            await asyncio.to_thread(pipeline.tick, store, cfg, datetime.now(UTC))
        except Exception as e:  # a bad tick must not kill ingest
            log.error("tick_failed", error=str(e))


def start_api(store: Store, cfg: Settings) -> Any:
    """Build the in-process API server (ADR-002) if heimdall.api.app is available."""
    try:
        from heimdall.api.app import create_app
    except ImportError:
        log.info("api_unavailable", note="heimdall.api.app not importable; running headless")
        return None
    import uvicorn

    class _Server(uvicorn.Server):
        def install_signal_handlers(self) -> None:  # runtime owns SIGINT/SIGTERM
            return

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:  # uvicorn >= 0.29
            yield

    config = uvicorn.Config(
        create_app(store), host=cfg.api.host, port=cfg.api.port, log_config=None
    )
    return _Server(config)


async def run(cfg: Settings | None = None, bus: Bus | None = None) -> None:
    cfg = cfg or load_settings()
    bus = bus or MemoryBus()
    lake = ParquetLake(cfg.storage.lake_dir)
    try:
        # ADR-002: analyst state lives in its own file so pipeline upserts never overwrite it
        analyst_path = Path(cfg.storage.duckdb_path).with_name("analyst.duckdb")
        store: Store = CompositeStore(
            DuckDBStore(cfg.storage.duckdb_path), DuckDBStore(analyst_path)
        )
    except duckdb.Error as e:
        # DuckDB holds an exclusive file lock (ADR-002): another heimdall process may own it.
        msg = (
            f"cannot open {cfg.storage.duckdb_path}: {e}\n"
            "Is another `heimdall.run` (or any process holding the DuckDB file) still running?"
        )
        log.error("store_unavailable", error=str(e), path=cfg.storage.duckdb_path)
        print(msg, file=sys.stderr)
        raise SystemExit(2) from e
    conns = build_connectors(cfg)
    writer = Writer(bus, lake, store)
    step_min = int(cfg.detectors.get("jamming", {}).get("step_min", 5))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(writer.consume()),
        asyncio.create_task(writer.flush_loop()),
        asyncio.create_task(health_loop(conns, store)),
        asyncio.create_task(tick_loop(store, cfg, step_min)),
        *(asyncio.create_task(pump(c, bus)) for c in conns),
    ]
    server = start_api(store, cfg)
    if server is not None:
        tasks.append(asyncio.create_task(server.serve()))
    log.info("started", connectors=[c.name for c in conns], step_min=step_min)
    await stop.wait()
    log.info("shutting_down")
    if server is not None:
        server.should_exit = True
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for c in conns:
        store.upsert_health(c.health())
        await c.aclose()
    writer.flush()
    await bus.close()
    store.close()


def main() -> None:
    configure_logging()
    with contextlib.suppress(OSError):
        start_metrics_server(9108)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
