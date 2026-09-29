"""Live runtime: connectors -> bus 'obs.air' -> writer (Lake + Store) -> periodic detection tick."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import signal
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import duckdb

from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_bus import MemoryBus
from heimdall.adapters.parquet_lake import ParquetLake
from heimdall.config import Settings
from heimdall.config import settings as load_settings
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


class Writer:
    """Buffers bus batches, de-duplicates across providers, writes Lake then Store."""

    def __init__(self, bus: Bus, lake: Lake, store: Store) -> None:
        self.bus, self.lake, self.store = bus, lake, store
        self.buf: list[AircraftObs] = []
        self.providers: dict[str, set[str]] = {}
        self.n_written = 0

    async def consume(self) -> None:
        async for batch in self.bus.subscribe(TOPIC_AIR):
            self.buf.extend(b for b in batch if isinstance(b, AircraftObs))

    def flush(self) -> int:
        if not self.buf:
            return 0
        rows, self.buf = self.buf, []
        kept, prov = merge_providers(rows)
        for hex_, p in prov.items():
            self.providers.setdefault(hex_, set()).update(p)
        self.lake.append("aircraft", kept)
        n = self.store.insert_aircraft(kept)
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
        records_last_hour=conn.n_records,
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
        store = DuckDBStore(cfg.storage.duckdb_path)
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
