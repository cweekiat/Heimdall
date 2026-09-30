"""Live runtime: connectors -> bus 'obs.air' -> writer (Lake + Store) -> periodic detection tick."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import signal
import sys
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from heimdall.adapters.composite_store import CompositeStore
from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_bus import MemoryBus
from heimdall.adapters.parquet_lake import ParquetLake
from heimdall.config import Settings
from heimdall.config import settings as load_settings
from heimdall.geo import haversine_km
from heimdall.ingest.adsb import AdsbAggregatorConnector, merge_providers
from heimdall.ingest.registry import build_connectors
from heimdall.logging import configure_logging, get_logger, start_metrics_server
from heimdall.ports import Bus, Lake, Store
from heimdall.schemas import AircraftObs, SourceHealth

log = get_logger("heimdall.run")

TOPIC_AIR = "obs.air"
FLUSH_S = 5.0
HEALTH_S = 60.0


async def pump(conn: AdsbAggregatorConnector, bus: Bus) -> None:
    async for batch in conn.stream():
        await bus.publish(TOPIC_AIR, batch)


DEDUPE_TTL_S = 30.0
DEDUPE_DT_S = 2.0
DEDUPE_KM = 0.5


class _Recent:
    __slots__ = ("kept", "last_seen", "providers")

    def __init__(self) -> None:
        # (observation ts, lat, lon, insertion time); expiry uses the insertion time
        self.kept: list[tuple[datetime, float, float, datetime]] = []
        self.providers: set[str] = set()
        self.last_seen: datetime = datetime.min.replace(tzinfo=UTC)  # insertion time


class Writer:
    """Buffers bus batches, de-duplicates across providers and flushes, writes Lake then Store.

    A rolling per-hex record of recently kept fixes (TTL `ttl_s`) catches duplicates that
    arrive in different flushes; the same TTL bounds the providers-seen side table.

    Expiry is keyed on the **insertion time** (the flush clock `now`), never on observation
    time: replayed/backtest data carries old observation timestamps, which would otherwise expire
    every key immediately and disable cross-flush dedupe. Duplicate matching still compares
    observation times (|dt| < 2 s). `flush` runs in a worker thread (`asyncio.to_thread`) while
    `consume` and `providers` run on the event loop, so all shared state (`buf`, `_recent`) is
    guarded by `_lock`; Lake/Store writes happen outside the lock.
    """

    def __init__(self, bus: Bus, lake: Lake, store: Store, ttl_s: float = DEDUPE_TTL_S) -> None:
        self.bus, self.lake, self.store = bus, lake, store
        self.ttl = timedelta(seconds=ttl_s)
        self.buf: list[AircraftObs] = []
        self._recent: dict[str, _Recent] = {}
        self._lock = threading.Lock()
        self.n_written = 0

    @property
    def providers(self) -> dict[str, set[str]]:
        with self._lock:
            return {h: set(r.providers) for h, r in self._recent.items()}

    def add(self, batch: Sequence[object]) -> None:
        with self._lock:
            self.buf.extend(b for b in batch if isinstance(b, AircraftObs))

    async def consume(self) -> None:
        async for batch in self.bus.subscribe(TOPIC_AIR):
            self.add(batch)

    def _is_dup(self, o: AircraftObs) -> bool:
        r = self._recent.get(o.hex)
        return r is not None and any(
            abs((o.ts - ts).total_seconds()) < DEDUPE_DT_S
            and haversine_km(o.lat, o.lon, lat, lon) < DEDUPE_KM
            for ts, lat, lon, _ in r.kept
        )

    def flush(self, now: datetime | None = None) -> int:
        """Dedupe and write buffered rows. `now` = insertion clock (wall clock by default)."""
        now = now or datetime.now(UTC)
        with self._lock:
            rows, self.buf = self.buf, []
            kept, prov = merge_providers(rows)
            for hex_, p in prov.items():
                r = self._recent.setdefault(hex_, _Recent())
                r.providers.update(p)
                r.last_seen = now
            fresh = [o for o in kept if not self._is_dup(o)]
            for o in fresh:
                self._recent[o.hex].kept.append((o.ts, o.lat, o.lon, now))
            # expire old state by insertion time (observation time may be arbitrarily old)
            for hex_ in [h for h, r in self._recent.items() if now - r.last_seen > self.ttl]:
                del self._recent[hex_]
            for r in self._recent.values():
                r.kept = [k for k in r.kept if now - k[3] <= self.ttl]
        if not fresh:
            return 0
        self.lake.append("aircraft", fresh)
        n = self.store.insert_aircraft(fresh)
        self.n_written += n
        return n

    async def flush_loop(self, interval: float = FLUSH_S) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await asyncio.to_thread(self.flush)
        finally:
            self.flush()


def health_for(conn: AdsbAggregatorConnector) -> SourceHealth:
    total = conn.n_ok + conn.n_err
    return SourceHealth(
        source=conn.name,
        domain=conn.domain,
        reliability=conn.reliability,
        last_ok=conn.last_ok,
        last_error=conn.last_error,
        last_error_at=conn.last_error_at,
        records_last_hour=conn.records_hour.total(),
        error_rate=conn.n_err / total if total else 0.0,
        expected_latency_s=conn.expected_latency_s,
    )


async def health_loop(conns: Sequence[AdsbAggregatorConnector], store: Store) -> None:
    while True:
        for c in conns:
            store.upsert_health(health_for(c))
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
        store.upsert_health(health_for(c))
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
