from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import httpx
import pytest
import respx

from heimdall import config
from heimdall import logging as hlog
from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_bus import MemoryBus
from heimdall.config import Settings
from heimdall.ingest.adsb import SlidingCounter
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient, TokenBucket
from heimdall.ingest.registry import build_connectors
from heimdall.run import Writer
from heimdall.schemas import AircraftObs

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


async def _nosleep(_: float) -> None:
    return None


@respx.mock
async def test_long_retry_after_opens_circuit_without_retrying() -> None:
    t = [0.0]
    route = respx.get("https://x.test/c").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "600"})
    )
    c = RateLimitedClient("t3", rps=1000, backoff_cap=60, sleep=_nosleep, clock=lambda: t[0])
    with pytest.raises(httpx.HTTPStatusError):
        await c.get("https://x.test/c")
    assert route.call_count == 1
    assert c.breaker.state == "open"
    t[0] = 599.0
    with pytest.raises(CircuitOpenError):
        await c.get("https://x.test/c")
    t[0] = 601.0
    assert c.breaker.state == "half_open"
    await c.aclose()


def test_rps_must_be_positive() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0)
    src = {"sources": {"adsb_lol": {"base": "https://x", "rps": 0}}}
    with pytest.raises(ValueError):
        build_connectors(Settings(), src)


def test_sliding_counter_expires() -> None:
    t = [0.0]
    c = SlidingCounter(3600, clock=lambda: t[0])
    c.add(5)
    t[0] = 1800
    c.add(3)
    assert c.total() == 8
    t[0] = 3700
    assert c.total() == 3
    t[0] = 6000
    assert c.total() == 0


def test_metrics_and_api_host_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEIMDALL_METRICS_HOST", raising=False)
    with mock.patch.object(hlog, "start_http_server") as m:
        hlog.start_metrics_server(9999)
        assert m.call_args.kwargs["addr"] == "127.0.0.1"
    all_ifaces = "0.0.0.0"
    monkeypatch.setenv("HEIMDALL_API_HOST", all_ifaces)
    config.settings.cache_clear()
    assert config.settings().api.host == all_ifaces
    monkeypatch.delenv("HEIMDALL_API_HOST")
    config.settings.cache_clear()
    assert config.settings().api.host == "127.0.0.1"
    config.settings.cache_clear()


class _Lake:
    def __init__(self) -> None:
        self.n = 0

    def append(self, dataset: str, rows: Any) -> int:
        self.n += len(rows)
        return len(rows)

    def paths(self, dataset: str, start: datetime, end: datetime) -> list[str]:
        return []


def _ac(hex_: str, dt: float, provider: str = "adsb.lol") -> AircraftObs:
    return AircraftObs(
        ts=T0 + timedelta(seconds=dt),
        hex=hex_,
        provider=provider,
        lat=10.0,
        lon=100.0,
        src_type="adsb_icao",
    )


def test_writer_dedupes_across_flushes() -> None:
    lake = _Lake()
    w = Writer(MemoryBus(), lake, DuckDBStore(), ttl_s=30)
    w.buf = [_ac("h1", 0)]
    assert w.flush(T0) == 1
    w.buf = [_ac("h1", 1, "adsb.fi"), _ac("h2", 1)]
    assert w.flush(T0 + timedelta(seconds=5)) == 1  # cross-flush duplicate dropped
    assert lake.n == 2
    assert w.providers["h1"] == {"adsb.lol", "adsb.fi"}
    assert w.flush(T0 + timedelta(seconds=120)) == 0  # TTL expiry bounds state
    assert w.providers == {}
