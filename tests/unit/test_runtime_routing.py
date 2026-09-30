"""Runtime routing by row type, sea/event writers, idempotent events, registry placeholders."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_bus import MemoryBus
from heimdall.adapters.memory_store import MemoryStore
from heimdall.config import Settings
from heimdall.ingest.adsb import AdsbAggregatorConnector
from heimdall.ingest.registry import build_connectors
from heimdall.run import (
    TOPIC_AIR,
    TOPIC_EVENTS,
    TOPIC_SEA,
    Writer,
    health_loop,
    pump,
    route,
)
from heimdall.schemas import (
    AircraftObs,
    Domain,
    GeoEvent,
    SourceHealth,
    SourceReliability,
    VesselObs,
)

T0 = datetime(2026, 9, 30, 6, 0, tzinfo=UTC)


def _air(hex_: str = "abc123") -> AircraftObs:
    return AircraftObs(
        ts=T0, hex=hex_, provider="adsb.lol", lat=3.0, lon=100.8, src_type="adsb_icao"
    )


def _sea(mmsi: int = 563000001, dt: float = 0.0) -> VesselObs:
    return VesselObs(
        ts=T0 + timedelta(seconds=dt), mmsi=mmsi, provider="aisstream", lat=3.0, lon=100.8
    )


def _evt(id_: str = "gfw:gap-1", value: float = 1.0) -> GeoEvent:
    return GeoEvent(
        id=id_,
        ts=T0,
        source="gfw",
        domain=Domain.SEA,
        kind="gfw_gap",
        lat=3.0,
        lon=100.8,
        value=value,
    )


class _Lake:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def append(self, dataset: str, rows: Sequence[BaseModel]) -> int:
        self.calls[dataset] = self.calls.get(dataset, 0) + len(rows)
        return len(rows)

    def paths(self, dataset: str, start: datetime, end: datetime) -> list[str]:
        return []


class _Mixed:
    name = "mixed"
    domain = Domain.SEA
    reliability = SourceReliability.B
    expected_latency_s = 1

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        yield [_air(), _sea(), _evt(), _sea(563000002)]

    def health(self) -> SourceHealth:
        return SourceHealth(
            source=self.name,
            domain=self.domain,
            reliability=self.reliability,
            status="degraded",
            detail="test",
        )

    async def aclose(self) -> None:
        return None


def test_route_splits_by_row_type() -> None:
    out = route([_air(), _sea(), _evt(), _sea(563000002)])
    assert {k: len(v) for k, v in out.items()} == {TOPIC_AIR: 1, TOPIC_SEA: 2, TOPIC_EVENTS: 1}


async def test_mixed_batch_reaches_correct_topics_and_tables() -> None:
    bus, lake, store = MemoryBus(), _Lake(), MemoryStore()
    w = Writer(bus, lake, store)
    consumer = asyncio.create_task(w.consume())
    await asyncio.sleep(0)  # let the subscriptions register
    seen: dict[str, list[Any]] = {}

    async def _tap(topic: str) -> None:
        async for b in bus.subscribe(topic):
            seen.setdefault(topic, []).extend(b)

    taps = [asyncio.create_task(_tap(t)) for t in (TOPIC_AIR, TOPIC_SEA, TOPIC_EVENTS)]
    await asyncio.sleep(0)
    await pump(_Mixed(), bus)
    await asyncio.sleep(0.05)
    assert {k: len(v) for k, v in seen.items()} == {TOPIC_AIR: 1, TOPIC_SEA: 2, TOPIC_EVENTS: 1}
    w.flush(T0)
    assert lake.calls == {"aircraft": 1, "vessels": 2, "events": 1}
    assert len(store.aircraft) == 1 and len(store.vessels) == 2 and len(store.events) == 1
    for t in [consumer, *taps]:
        t.cancel()
    await asyncio.gather(consumer, *taps, return_exceptions=True)


def test_sea_dedupe_on_mmsi_and_ts_with_wall_ttl() -> None:
    lake, store = _Lake(), MemoryStore()
    w = Writer(MemoryBus(), lake, store, ttl_s=30)
    w.sea_buf = [_sea(dt=0), _sea(dt=0), _sea(dt=5)]
    w.flush(T0)
    assert len(store.vessels) == 2
    w.sea_buf = [_sea(dt=0)]
    w.flush(T0 + timedelta(seconds=10))
    assert len(store.vessels) == 2  # duplicate inside TTL dropped
    w.sea_buf = [_sea(dt=0)]
    w.flush(T0 + timedelta(seconds=100))  # TTL is on insertion (wall) time, not obs time
    assert len(store.vessels) == 3


def test_air_dedupe_ttl_uses_insertion_time() -> None:
    w = Writer(MemoryBus(), _Lake(), MemoryStore(), ttl_s=30)
    old = _air().model_copy(update={"ts": T0 - timedelta(hours=1)})  # late-arriving old obs
    w.buf = [old]
    assert w.flush(T0) == 1
    w.buf = [old.model_copy()]
    assert w.flush(T0 + timedelta(seconds=5)) == 0  # still remembered: not expired by obs age


@pytest.mark.parametrize("make", [lambda: DuckDBStore(":memory:"), MemoryStore])
def test_insert_events_is_idempotent_on_id(make: Any) -> None:
    s = make()
    assert s.insert_events([_evt(value=1.0)]) == 1
    s.insert_events([_evt(value=2.0), _evt("other")])
    rows = s.events_window(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert sorted(r.id for r in rows) == ["gfw:gap-1", "other"]
    assert next(r for r in rows if r.id == "gfw:gap-1").value == 2.0  # last write wins
    s.insert_events([_evt(value=3.0), _evt(value=4.0)])  # duplicates inside one batch
    rows = s.events_window(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert [r.value for r in rows if r.id == "gfw:gap-1"] == [4.0]


def test_registry_unknown_impl_is_disabled_placeholder() -> None:
    src = {
        "sources": {
            "future": {
                "domain": "sea",
                "reliability": "A",
                "latency_s": 60,
                "impl": "heimdall.ingest.does_not_exist:build",
            },
            "bad_attr": {"domain": "air", "impl": "heimdall.ingest.ais:nope"},
            "off": {"domain": "sea", "impl": "heimdall.ingest.ais:build", "enabled": False},
        }
    }
    conns = build_connectors(Settings(), src)
    assert [c.name for c in conns] == ["future", "bad_attr"]  # enabled:false stays out
    for c in conns:
        h = c.health()
        assert h.status == "disabled" and h.detail == "not installed"
    assert conns[0].domain == Domain.SEA and conns[0].reliability == SourceReliability.A


def test_registry_reserved_impls_show_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from heimdall.config import sources

    monkeypatch.delenv("AISSTREAM_KEY", raising=False)
    monkeypatch.delenv("GFW_TOKEN", raising=False)
    by_name = {c.name: c for c in build_connectors(Settings(), sources())}
    assert by_name["aisstream"].health().status == "needs_key"
    assert by_name["gfw"].health().status == "needs_key"
    for n in ("nga_msi", "china_msa", "faa_nms_notam"):  # modules land with task 009
        if n in by_name:
            assert by_name[n].health().status in ("disabled", "needs_key", "ok", "degraded")


def test_adsb_connector_implements_health() -> None:
    c = AdsbAggregatorConnector("adsb_lol", "https://x.invalid", "/v2/{lat}/{lon}/{radius}", [])
    h = c.health()
    assert h.source == "adsb_lol" and h.domain == Domain.AIR and h.status == "ok"
    c.last_error, c.last_error_at = "boom", datetime.now(UTC)
    assert c.health().status == "degraded"


async def test_health_loop_uses_connector_health() -> None:
    store = MemoryStore()
    t = asyncio.create_task(health_loop([_Mixed()], store))
    await asyncio.sleep(0.02)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    assert [(h.source, h.status, h.detail) for h in store.list_health()] == [
        ("mixed", "degraded", "test")
    ]


async def test_pump_survives_connector_crash() -> None:
    class _Boom(_Mixed):
        async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
            raise RuntimeError("kaput")
            yield []  # pragma: no cover

    await pump(_Boom(), MemoryBus())  # logs, does not raise


async def test_sea_and_events_keep_flowing_after_first_flush() -> None:
    """Regression: flush() swaps the buffers; later batches must still be written."""
    bus, lake, store = MemoryBus(), _Lake(), MemoryStore()
    w = Writer(bus, lake, store)
    consumer = asyncio.create_task(w.consume())
    await asyncio.sleep(0.01)  # let the subscriptions register
    await bus.publish(TOPIC_SEA, [_sea(563000001)])
    await asyncio.sleep(0.01)
    w.flush(T0)
    await bus.publish(TOPIC_SEA, [_sea(563000002)])
    await bus.publish(TOPIC_EVENTS, [_evt()])
    await asyncio.sleep(0.01)
    w.flush(T0 + timedelta(seconds=2))
    assert len(store.vessels) == 2 and len(store.events) == 1
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
