"""Live runtime: connectors -> bus 'obs.air' -> writer (Lake + Store) -> periodic detection tick."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import itertools
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel

from heimdall.adapters.aoi_file import FileAoiStore
from heimdall.adapters.composite_store import CompositeStore
from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.gnss_lake import GnssLake
from heimdall.adapters.memory_bus import MemoryBus
from heimdall.adapters.parquet_lake import ParquetLake
from heimdall.adapters.secrets_file import default_store
from heimdall.aoi_ctl import AoiService
from heimdall.config import Settings
from heimdall.config import settings as load_settings
from heimdall.config import sources as load_sources
from heimdall.geo import haversine_km
from heimdall.gnss_service import GnssService, LakeGnssService, gnss_loop
from heimdall.ingest.adsb import merge_providers
from heimdall.ingest.registry import ADSB_PROVIDERS, build_connectors
from heimdall.live import LivePositions
from heimdall.logging import configure_logging, get_logger, start_metrics_server
from heimdall.ports import Bus, Connector, Lake, SourceControl, Store
from heimdall.schemas import AircraftObs, GeoEvent, VesselObs
from heimdall.sources_ctl import SourceManager

log = get_logger("heimdall.run")

TOPIC_AIR = "obs.air"
TOPIC_SEA = "obs.sea"
TOPIC_EVENTS = "events"
FLUSH_S = 2.0  # live latency: obs reach the Store (and the map) within ~2 s of fetch
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


START_SLOT_S = 2.5  # 057: connectors start one slot apart (no 420/429 burst on shared providers)
START_SPREAD_S = 30.0
_slots = itertools.count()


def start_offset() -> float:
    """Next start delay: 0, 2.5, 5, ... wrapping at START_SPREAD_S (connectors started later,
    e.g. switched on in Data sources, take the next slot)."""
    return (next(_slots) * START_SLOT_S) % START_SPREAD_S


async def pump(conn: Connector, bus: Bus, delay_s: float | None = None) -> None:
    try:
        await asyncio.sleep(start_offset() if delay_s is None else delay_s)
        async for batch in conn.stream():
            for topic, rows in route(batch).items():
                await bus.publish(topic, rows)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # one broken source must not stop the others
        log.error("connector_crashed", source=conn.name, error=f"{type(e).__name__}: {e}")


GLOBAL_AIR = "global/aircraft"  # 024g lake partitions (display-only, short retention)
GLOBAL_SEA = "global/vessels"
GLOBAL_SEEN_TTL = timedelta(minutes=10)
JANITOR_S = 600.0  # small, frequent purges (an hourly one deleted 6.6 M rows at once)

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

    def __init__(
        self,
        bus: Bus,
        lake: Lake,
        store: Store,
        ttl_s: float = DEDUPE_TTL_S,
        live: LivePositions | None = None,
    ) -> None:
        self.bus, self.lake, self.store = bus, lake, store
        self.live = live  # 026a: latest-fix index the API serves traffic from
        self.ttl = timedelta(seconds=ttl_s)
        self.buf: list[AircraftObs] = []
        self.sea_buf: list[VesselObs] = []
        self.evt_buf: list[GeoEvent] = []
        self._sea_seen: dict[tuple[int, datetime], datetime] = {}  # (mmsi, ts) -> inserted (wall)
        self._glob_seen: dict[tuple[str, datetime], datetime] = {}  # 024g (hex, ts) -> inserted
        self.n_sea = 0
        self.n_global = 0
        self.n_events = 0
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
        async def _one(topic: str, attr: str, typ: type[BaseModel]) -> None:
            # look the buffer up on every batch: flush() swaps in a new list, so a list
            # captured once would keep collecting rows that are never written
            async for batch in self.bus.subscribe(topic):
                rows = [b for b in batch if isinstance(b, typ)]
                with self._lock:
                    getattr(self, attr).extend(rows)

        async def _air() -> None:
            async for batch in self.bus.subscribe(TOPIC_AIR):
                self.add(batch)

        await asyncio.gather(
            _air(),
            _one(TOPIC_SEA, "sea_buf", VesselObs),
            _one(TOPIC_EVENTS, "evt_buf", GeoEvent),
        )

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
            all_rows, self.buf = self.buf, []
            # 024g: global (OpenSky) rows skip cross-provider merge so they can never displace an
            # ADS-B fix carrying NIC/NACp; they only need an exact (hex, ts) repeat filter.
            rows = [o for o in all_rows if not o.is_global]
            glob = [o for o in all_rows if o.is_global]
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
            provs = {o.hex: set(self._recent[o.hex].providers) for o in fresh}
        if self.live is not None:
            self.live.update_air(fresh, now, provs)
            self.live.expire(now)
        self._flush_sea(now)
        self._flush_events()
        self._flush_global_air(glob, now)
        if not fresh:
            return 0
        self.lake.append("aircraft", fresh)
        n = self.store.insert_aircraft(fresh)
        self.n_written += n
        return n

    def _flush_global_air(self, rows: Sequence[AircraftObs], now: datetime) -> None:
        if not rows:
            return
        for k in [k for k, t in self._glob_seen.items() if now - t > GLOBAL_SEEN_TTL]:
            del self._glob_seen[k]
        fresh: list[AircraftObs] = []
        for o in rows:
            if (o.hex, o.ts) not in self._glob_seen:
                self._glob_seen[(o.hex, o.ts)] = now
                fresh.append(o)
        if fresh:
            if self.live is not None:
                self.live.update_air(fresh, now)
            self.lake.append(GLOBAL_AIR, fresh)
            self.n_global += self.store.insert_aircraft(fresh)

    def _flush_sea(self, now: datetime) -> None:
        with self._lock:
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
            if self.live is not None:
                self.live.update_sea(fresh, now)
            local = [v for v in fresh if not v.is_global]
            if local:
                self.lake.append("vessels", local)
            if len(local) < len(fresh):  # 024g: outside-AOI rows live in the global partition
                self.lake.append(GLOBAL_SEA, [v for v in fresh if v.is_global])
            self.n_sea += self.store.insert_vessels(fresh)

    def _flush_events(self) -> None:
        with self._lock:
            rows, self.evt_buf = self.evt_buf, []
        if rows:  # Lake keeps every revision; the Store upserts by id (ADR-004)
            self.lake.append("events", rows)
            self.n_events += self.store.insert_events(rows)

    async def flush_loop(self, interval: float = FLUSH_S) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await asyncio.to_thread(self.flush)
                except Exception as e:  # one failed write must not stop ingest for good
                    log.error("flush_failed", error=f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            with contextlib.suppress(Exception):
                self.flush()


async def janitor_loop(
    lake: Lake,
    store: Store,
    retention_days: int,
    hot_h: float | None = None,
    hot_events_h: float | None = None,
    hot_global_h: float | None = None,
) -> None:
    """024g: drop display-only global data older than `retention_days` (lake) or `hot_global_h`
    (hot store); AOI data is untouched."""
    while True:
        try:
            cutoff = datetime.now(UTC) - timedelta(days=retention_days)
            purge = getattr(lake, "purge_global", None)
            n_files = await asyncio.to_thread(purge, cutoff) if purge else 0
            fn = getattr(store, "purge_global", None)
            store_cut = (
                datetime.now(UTC) - timedelta(hours=hot_global_h) if hot_global_h else cutoff
            )
            n_rows = await asyncio.to_thread(fn, store_cut) if fn else 0
            if n_files or n_rows:
                log.info("janitor_global", lake_dirs=n_files, store_rows=n_rows)
            hot = getattr(store, "purge_hot", None)
            if hot is not None and hot_h is not None:  # 026a: bound the hot store
                now = datetime.now(UTC)
                ev = now - timedelta(hours=hot_events_h) if hot_events_h else None
                n_hot = await asyncio.to_thread(hot, now - timedelta(hours=hot_h), ev)
                if n_hot:
                    log.info("janitor_hot", store_rows=n_hot)
        except Exception as e:  # housekeeping must never take the runtime down
            log.warning("janitor_failed", error=f"{type(e).__name__}: {e}")
        await asyncio.sleep(JANITOR_S)


async def health_loop(
    conns: Sequence[Connector] | Callable[[], Sequence[Connector]], store: Store
) -> None:
    while True:
        try:
            for c in conns() if callable(conns) else conns:
                store.upsert_health(c.health())
        except Exception as e:  # health bookkeeping must never end the loop
            log.warning("health_failed", error=f"{type(e).__name__}: {str(e)[:300]}")
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


def start_api(
    store: Store,
    cfg: Settings,
    sources: SourceControl | None = None,
    aoi_service: AoiService | None = None,
    live: LivePositions | None = None,
    gnss: GnssService | None = None,
) -> Any:
    """Build the in-process API server (ADR-002) if heimdall.api.app is available."""
    try:
        from heimdall.api.access import gate_from_settings
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
        create_app(
            store,
            sources=sources,
            secrets=default_store(),
            access=gate_from_settings(cfg),
            allowed_hosts=(
                [*cfg.api.allowed_hosts, "127.0.0.1", "localhost"]
                if cfg.api.allowed_hosts
                else None
            ),
            aoi_service=aoi_service,
            live=live,
            gnss=gnss,
        ),
        host=cfg.api.host,
        port=cfg.api.port,
        log_config=None,
        timeout_graceful_shutdown=3,  # open SSE streams must not hold shutdown
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
            DuckDBStore(
                cfg.storage.duckdb_path,
                memory_limit=cfg.storage.duckdb_memory_limit,
                threads=cfg.storage.duckdb_threads,
            ),
            DuckDBStore(analyst_path, memory_limit="256MB", threads=1),
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
    if not cfg.use_config_aois:
        # areas of operation come only from what the analyst draws (data/state/aois.json)
        cfg.aois = [a for a in cfg.aois if a.origin != "config"]
    src_cfg = load_sources()
    # only sources that can run and are not retired are managed (and listed in Data sources)
    all_src = {
        k: v
        for k, v in src_cfg.get("sources", {}).items()
        if (k in ADSB_PROVIDERS or "impl" in v) and not v.get("retired")
    }

    def build_one(key: str) -> Connector | None:
        # a toggled-on source overrides `enabled: false` from sources.yaml
        forced = {"sources": {key: {**all_src[key], "enabled": True}}}
        built = build_connectors(cfg, forced, enabled={key})
        return built[0] if built else None

    manager = SourceManager(
        all_src,
        build_one,
        lambda c: pump(c, bus),
        Path(cfg.storage.duckdb_path).parent / "state" / "sources.json",
    )
    # user-drawn areas load before manager.start() so the first poll already includes them
    aoi_svc = AoiService(
        FileAoiStore(Path(cfg.storage.duckdb_path).parent / "state" / "aois.json"),
        cfg,
        manager,
        all_src,
        background=True,
    )
    live = LivePositions()
    writer = Writer(bus, lake, store, live=live)
    gnss = LakeGnssService(
        GnssLake(cfg.storage.lake_dir), cfg.detectors, sorted(p for p, _ in ADSB_PROVIDERS.values())
    )  # task 031: GPSJam-style map from the lake
    step_min = int(cfg.detectors.get("jamming", {}).get("step_min", 5))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_signal() -> None:
        # 1st Ctrl+C: graceful shutdown. 2nd: exit now (a stalled shutdown must never leave a
        # half-dead process holding the API port).
        if stop.is_set():
            log.warning("forced_exit")
            os._exit(130)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, on_signal)

    tasks = [
        asyncio.create_task(writer.consume()),
        asyncio.create_task(writer.flush_loop()),
        asyncio.create_task(
            janitor_loop(
                lake,
                store,
                cfg.storage.global_retention_days,
                cfg.storage.hot_retention_h,
                cfg.storage.hot_retention_events_h,
                cfg.storage.hot_retention_global_h,
            )
        ),
        asyncio.create_task(health_loop(lambda: manager.connectors, store)),
        asyncio.create_task(tick_loop(store, cfg, step_min)),
        asyncio.create_task(gnss_loop(gnss)),
    ]
    await manager.start()
    server = start_api(store, cfg, manager, aoi_svc, live, gnss)
    server_task = asyncio.create_task(server.serve()) if server is not None else None
    log.info("started", connectors=[c.name for c in manager.connectors], step_min=step_min)
    await stop.wait()
    await shutdown(manager, server, server_task, tasks, store, writer, bus)


SHUTDOWN_DEADLINE_S = 20.0


async def shutdown(
    manager: SourceManager,
    server: Any,
    server_task: asyncio.Task[None] | None,
    tasks: Sequence[asyncio.Task[Any]],
    store: Store,
    writer: Writer,
    bus: Bus,
    deadline_s: float = SHUTDOWN_DEADLINE_S,
) -> None:
    """Stop in the order that ends outside effects first: external polling, then the API port."""
    log.info("shutting_down")
    # hard deadline: a hung thread or socket must not keep the process (and port 8000) alive
    watchdog = threading.Timer(deadline_s, lambda: (log.error("shutdown_timeout"), os._exit(1)))
    watchdog.daemon = True
    watchdog.start()
    # 1) no more requests to external sources (all connectors at once)
    for c in manager.connectors:
        with contextlib.suppress(Exception):
            store.upsert_health(c.health())
    try:
        await asyncio.wait_for(manager.stop(), timeout=8)
    except TimeoutError:
        log.warning("connectors_stop_timeout")
    # 2) close the API port the uvicorn way (stop accepting, end streams), not by cancellation
    if server is not None and server_task is not None:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(server_task), timeout=5)
        except TimeoutError:
            server.force_exit = True
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
    # 3) background loops, then flush what the writer holds
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    writer.flush()
    await bus.close()
    store.close()
    watchdog.cancel()
    log.info("stopped")


def main() -> None:
    configure_logging()
    with contextlib.suppress(OSError):
        start_metrics_server(9108)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
