from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from heimdall.config import AOI
from heimdall.ingest.adsb import AdsbAggregatorConnector, merge_providers
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.ingest.readsb import parse_readsb_v2
from heimdall.schemas import AircraftObs

FIX = Path(__file__).parent / "fixtures" / "adsb_lol" / "point_20.5_96.5_250_myanmar.json"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_parse_fixture() -> None:
    payload = json.loads(FIX.read_text())
    obs, dropped = parse_readsb_v2(payload, "adsb.lol", NOW)
    assert dropped == 0
    assert len(obs) == len([a for a in payload["ac"] if "lat" in a])
    o = obs[0]
    assert o.hex == "7811b2" and o.flight == "CQH8980"
    assert o.ts == NOW - timedelta(seconds=3.139)
    assert o.h3_r4 and o.h3_r6 and o.is_true_adsb


def test_parse_edge_cases() -> None:
    payload = {
        "ac": [
            {"hex": "AAAAAA", "type": "mlat", "lat": 10, "lon": 100, "alt_baro": 5000},
            {"hex": "bbbbbb", "type": "adsb_icao", "lat": 1, "lon": 1, "alt_baro": "ground"},
            {"hex": "cccccc", "type": "adsb_icao", "lat": 95, "lon": 1},  # bad lat
            {"hex": "dddddd", "type": "adsb_icao", "lat": 5, "lon": 5, "gs": 5000},  # too fast
            {"hex": "eeeeee", "type": "adsb_icao", "lat": 5, "lon": 5, "alt_baro": 900000},
            {"hex": "ffffff", "type": "mode_s"},  # no position: skipped, not counted
            {"hex": "123456", "type": "adsb_icao", "lat": 5, "lon": 5, "dbFlags": 1},
        ]
    }
    obs, dropped = parse_readsb_v2(payload, "adsb.lol", NOW)
    assert dropped == 3
    by = {o.hex: o for o in obs}
    assert set(by) == {"aaaaaa", "bbbbbb", "123456"}
    assert not by["aaaaaa"].is_true_adsb
    assert by["bbbbbb"].on_ground and by["bbbbbb"].alt_baro is None
    assert by["123456"].mil and not by["aaaaaa"].mil


def _obs(hex_: str, provider: str, dt: float, lat: float = 10.0) -> AircraftObs:
    return AircraftObs(
        ts=NOW + timedelta(seconds=dt), hex=hex_, provider=provider, lat=lat, lon=100.0,
        src_type="adsb_icao",
    )  # fmt: skip


def test_merge_providers() -> None:
    rows = [
        _obs("a", "adsb.lol", 0),
        _obs("a", "adsb.fi", 1.0),  # dup
        _obs("a", "airplanes.live", 1.5, lat=10.0001),  # dup
        _obs("a", "adsb.lol", 10),  # later: kept
        _obs("a", "adsb.fi", 0.5, lat=11.0),  # far away: kept
        _obs("b", "adsb.fi", 0),
    ]
    kept, prov = merge_providers(rows)
    assert len(kept) == 4
    assert prov["a"] == {"adsb.lol", "adsb.fi", "airplanes.live"}
    assert prov["b"] == {"adsb.fi"}


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


@respx.mock
async def test_backoff_on_429_honours_retry_after() -> None:
    sleep = FakeSleep()
    route = respx.get("https://x.test/a").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    c = RateLimitedClient("t", rps=1000, sleep=sleep)
    r = await c.get("https://x.test/a")
    assert r.json() == {"ok": 1} and route.call_count == 3
    assert sleep.calls[0] == 7.0
    assert 0 <= sleep.calls[1] <= 2.0  # jittered exp backoff (base 1, attempt 1)
    assert respx.calls[0].request.headers["user-agent"] == "heimdall-osint/0.1"
    await c.aclose()


@respx.mock
async def test_circuit_breaker_opens_and_half_opens() -> None:
    t = [0.0]
    respx.get("https://x.test/b").mock(return_value=httpx.Response(500))

    async def sleep(s: float) -> None:  # fake clock must advance or the bucket never refills
        t[0] += s

    c = RateLimitedClient("t2", rps=1000, max_retries=4, sleep=sleep, clock=lambda: t[0])
    with pytest.raises(httpx.HTTPStatusError):
        await c.get("https://x.test/b")  # 5 failures -> open
    assert c.breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        await c.get("https://x.test/b")
    t[0] += 61.0
    assert c.breaker.state == "half_open"
    respx.get("https://x.test/b").mock(return_value=httpx.Response(200, json={}))
    await c.get("https://x.test/b")
    assert c.breaker.state == "closed"
    await c.aclose()


async def test_token_bucket_spaces_requests() -> None:
    from heimdall.ingest.http import TokenBucket

    t = [0.0]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        t[0] += s

    b = TokenBucket(2.0, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        await b.acquire()
    assert slept == [pytest.approx(0.5), pytest.approx(0.5)]


@respx.mock
async def test_connector_polls_and_round_robins() -> None:
    aois = [AOI(name="m", lat=20.5, lon=96.5, radius_nm=250), AOI(name="b", lat=56, lon=20.5,
                                                                    radius_nm=250)]  # fmt: skip
    payload = json.loads(FIX.read_text())
    r1 = respx.get("https://api.test/v2/point/20.5/96.5/250").mock(
        return_value=httpx.Response(200, json=payload)
    )
    r2 = respx.get("https://api.test/v2/point/56.0/20.5/250").mock(
        return_value=httpx.Response(200, json={"ac": [], "now": 1})
    )
    conn = AdsbAggregatorConnector(
        "adsb_lol", "https://api.test", "/v2/point/{lat}/{lon}/{radius}", aois, rps=1000
    )
    a = await conn.poll_once()
    b = await conn.poll_once()
    assert len(a) == 3 and b == [] and r1.called and r2.called
    assert conn.n_ok == 2 and conn.provider == "adsb.lol"
    await conn.aclose()
