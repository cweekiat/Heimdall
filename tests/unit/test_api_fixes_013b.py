"""Task 013b: API/source-control review fixes (offline, TestClient)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.ports import Connector
from heimdall.schemas import AircraftObs, Domain, SourceHealth, SourceReliability, VesselObs
from heimdall.sources_ctl import SourceManager

HOSTS = ["testserver", "127.0.0.1", "localhost"]
PB: dict[str, Any] = {"actions": {}}
NOW = datetime.now(UTC)
OH = {"Origin": "http://testserver"}


def _ac(hex_: str, lat: float, lon: float, dt: float = 5) -> AircraftObs:
    return AircraftObs(
        ts=NOW - timedelta(seconds=dt),
        hex=hex_,
        provider="adsb.lol",
        lat=lat,
        lon=lon,
        src_type="adsb_icao",
    )


# ------------------------------------------------------------------ sources_ctl
class Conn:
    domain = Domain.AIR
    reliability = SourceReliability.B
    expected_latency_s = 1

    def __init__(self, name: str, fail_close: bool = False) -> None:
        self.name = name
        self.fail_close = fail_close

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        await asyncio.sleep(3600)
        yield []

    def health(self) -> SourceHealth:
        return SourceHealth(source=self.name, domain=self.domain, reliability=self.reliability)

    async def aclose(self) -> None:
        if self.fail_close:
            raise RuntimeError("boom")


def _mgr(path: Path, pump: Any, fail_close: bool = False) -> tuple[SourceManager, list[Conn]]:
    built: list[Conn] = []

    def build(key: str) -> Connector:
        c = Conn(key, fail_close)
        built.append(c)
        return c

    return SourceManager({"alpha": {"domain": "air"}}, build, pump, path), built


async def test_dead_pump_is_reported_and_restarted(tmp_path: Path) -> None:
    runs = {"n": 0}

    async def pump(c: Connector) -> None:
        runs["n"] += 1
        if runs["n"] == 1:
            raise RuntimeError("pump died")
        await asyncio.sleep(3600)

    m, built = _mgr(tmp_path / "s.json", pump)
    await m.start()
    await asyncio.sleep(0.05)
    assert m.list()[0].status == "down"
    st = await m.set_enabled("alpha", True)  # already enabled, but the task is dead
    await asyncio.sleep(0.01)
    assert runs["n"] == 2 and len(built) == 2 and st.status == "ok"
    await m.stop()


async def test_orphaned_connectors_are_closed(tmp_path: Path) -> None:
    runs = {"n": 0}
    closed: list[str] = []

    class Tracked(Conn):
        async def aclose(self) -> None:
            closed.append(self.name)
            raise RuntimeError("close failed")  # must be logged, not raised

    async def pump(c: Connector) -> None:
        runs["n"] += 1
        if runs["n"] == 1:
            raise RuntimeError("pump died")
        await asyncio.sleep(3600)

    built: list[Tracked] = []

    def build(key: str) -> Connector:
        c = Tracked(f"{key}{len(built)}")
        built.append(c)
        return c

    m = SourceManager({"alpha": {"domain": "air"}}, build, pump, tmp_path / "s.json")
    await m.start()
    await asyncio.sleep(0.05)
    await m.set_enabled("alpha", True)  # restart -> the dead connector is closed
    assert closed == ["alpha0"]
    await m.stop()
    assert closed == ["alpha0", "alpha1"]


async def test_aclose_failure_still_persists(tmp_path: Path) -> None:
    async def pump(c: Connector) -> None:
        await asyncio.sleep(3600)

    path = tmp_path / "s.json"
    m, _ = _mgr(path, pump, fail_close=True)
    await m.start()
    st = await m.set_enabled("alpha", False)
    assert st.status == "off" and json.loads(path.read_text()) == {"alpha": False}


async def test_state_file_private_atomic_and_malformed_ignored(tmp_path: Path) -> None:
    async def pump(c: Connector) -> None:
        await asyncio.sleep(3600)

    path = tmp_path / "state" / "sources.json"
    m, _ = _mgr(path, pump)
    await m.set_enabled("alpha", False)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not path.with_suffix(".tmp").exists()
    for junk in ("[1, 2]", '"x"', "null", "{not json"):
        path.write_text(junk)
        m2, _ = _mgr(path, pump)  # must not raise
        assert m2.list()[0].enabled is True


# ------------------------------------------------------------------ store filters
def test_store_bbox_and_ident_filters_match_between_adapters(tmp_path: Path) -> None:
    rows = [_ac("aaaaaa", 5, 5), _ac("bbbbbb", 50, 50), _ac("cccccc", 5, 179.5)]
    vs = [
        VesselObs(ts=NOW - timedelta(seconds=5), mmsi=1, provider="aisstream", lat=5, lon=5),
        VesselObs(ts=NOW - timedelta(seconds=5), mmsi=2, provider="aisstream", lat=50, lon=50),
    ]
    mem, db = MemoryStore(), DuckDBStore(str(tmp_path / "t.duckdb"))
    for s in (mem, db):
        s.insert_aircraft(rows)
        s.insert_vessels(vs)
    a, b = NOW - timedelta(minutes=1), NOW + timedelta(minutes=1)
    for s in (mem, db):
        assert [o.hex for o in s.aircraft_window(a, b, bbox=(0, 0, 10, 10))] == ["aaaaaa"]
        assert [o.hex for o in s.aircraft_window(a, b, bbox=(170, 0, -170, 10))] == ["cccccc"]
        assert [o.hex for o in s.aircraft_window(a, b, ident="bbbbbb")] == ["bbbbbb"]
        assert [v.mmsi for v in s.vessels_window(a, b, ident=2)] == [2]
        assert [v.mmsi for v in s.vessels_window(a, b, bbox=(0, 0, 10, 10))] == [1]


def test_traffic_uses_store_filters_and_caches_evidence_lookup() -> None:
    s = MemoryStore()
    s.insert_aircraft([_ac("aabbcc", 5, 5), _ac("dddddd", 50, 50)])
    calls: dict[str, Any] = {"ev": 0, "win": []}
    orig_ev, orig_win = s.get_evidence, s.aircraft_window

    def get_ev(ids: Any) -> Any:
        calls["ev"] += 1
        return orig_ev(ids)

    def win(*a: Any, **k: Any) -> Any:
        calls["win"].append(k)
        return orig_win(*a, **k)

    s.get_evidence, s.aircraft_window = get_ev, win  # type: ignore[method-assign]
    c = TestClient(create_app(s, playbook=PB, allowed_hosts=HOSTS))
    for _ in range(3):
        fc = c.get("/api/traffic/air?bbox=0,0,10,10").json()
    assert [f["properties"]["hex"] for f in fc["features"]] == ["aabbcc"]
    assert calls["ev"] <= 1
    assert calls["win"][0]["bbox"] == (0.0, 0.0, 10.0, 10.0)
    assert c.get("/api/traffic/air/aabbcc/trail").status_code == 200
    assert calls["win"][-1]["ident"] == "aabbcc"


def test_no_ghost_when_newest_fix_left_the_bbox(tmp_path: Path) -> None:
    for s in (MemoryStore(), DuckDBStore(str(tmp_path / "g.duckdb"))):
        s.insert_aircraft(
            [_ac("aabbcc", 5, 5, dt=30), _ac("aabbcc", 50, 50, dt=5), _ac("dddddd", 6, 6, dt=5)]
        )
        s.insert_vessels(
            [
                VesselObs(
                    ts=NOW - timedelta(seconds=30), mmsi=7, provider="aisstream", lat=5, lon=5
                ),
                VesselObs(
                    ts=NOW - timedelta(seconds=5), mmsi=7, provider="aisstream", lat=50, lon=50
                ),
            ]
        )
        c = TestClient(create_app(s, playbook=PB, allowed_hosts=HOSTS))
        air = c.get("/api/traffic/air?bbox=0,0,10,10&max_age_s=120").json()
        assert [f["properties"]["hex"] for f in air["features"]] == ["dddddd"]
        sea = c.get("/api/traffic/sea?bbox=0,0,10,10&max_age_s=120").json()
        assert sea["features"] == []


# ------------------------------------------------------------------ validation / headers / csrf
@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(MemoryStore(), playbook=PB, allowed_hosts=HOSTS))


def test_actions_since_h_and_notices_at_validated(client: TestClient) -> None:
    for v in ("nan", "inf", "1e999", "0", "-1", "721"):
        assert client.get(f"/api/actions?since_h={v}").status_code == 422, v
    assert client.get("/api/actions?since_h=24").status_code == 200
    for at in ("0001-01-01", "9999-12-31T00:00:00Z"):
        assert client.get("/api/notices", params={"at": at}).status_code == 422, at
    assert client.get("/api/notices", params={"at": NOW.isoformat()}).status_code == 200


def test_security_headers_and_csp(client: TestClient) -> None:
    r = client.get("/api/health")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "https://tiles.openfreemap.org" in csp and "worker-src 'self' blob:" in csp
    assert "img-src 'self' data: blob:" in csp and "cartocdn" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    carto = TestClient(create_app(MemoryStore(), playbook=PB, allowed_hosts=HOSTS, basemap="carto"))
    assert "basemaps.cartocdn.com" in carto.get("/api/health").headers["content-security-policy"]
    none = TestClient(create_app(MemoryStore(), playbook=PB, allowed_hosts=HOSTS, basemap="none"))
    assert "https://" not in none.get("/api/health").headers["content-security-policy"]


def test_writes_require_origin_or_same_origin_fetch(client: TestClient) -> None:
    url, body = "/api/alerts/x/feedback", {"verdict": "confirm"}
    assert client.post(url, json=body).status_code == 403
    assert client.post(url, json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(url, json=body, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 404
    assert client.post(url, json=body, headers=OH).status_code == 404
