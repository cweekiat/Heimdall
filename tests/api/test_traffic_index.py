"""026a: live index, delta/compact responses, bulk insert and hot retention (offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.api.traffic import traffic_router
from heimdall.live import LivePositions
from heimdall.schemas import AircraftObs, VesselObs

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
WORLD = "-180,-85,180,85"


def air(hex_: str, lat: float, lon: float, age: float = 5, mil: bool = False) -> AircraftObs:
    return AircraftObs(
        ts=NOW - timedelta(seconds=age),
        hex=hex_,
        provider="adsb.lol",
        lat=lat,
        lon=lon,
        src_type="adsb_icao",
        mil=mil,
    )


def sea(mmsi: int, lat: float, lon: float) -> VesselObs:
    return VesselObs(
        ts=NOW - timedelta(seconds=30), mmsi=mmsi, provider="aisstream", lat=lat, lon=lon
    )


def client(idx: LivePositions | None) -> TestClient:
    app = FastAPI()
    app.include_router(traffic_router(DuckDBStore(":memory:"), now=lambda: NOW, index=idx))
    return TestClient(app)


def test_index_geojson_and_total() -> None:
    idx = LivePositions()
    idx.update_air([air("aaaaaa", 1.0, 103.0), air("bbbbbb", 50.0, 10.0, mil=True)], NOW)
    c = client(idx)
    r = c.get("/api/traffic/air", params={"bbox": "100,0,110,5"}).json()
    assert [f["properties"]["hex"] for f in r["features"]] == ["aaaaaa"]
    assert r["total"] == 1 and r["truncated"] is False
    assert r["features"][0]["geometry"]["coordinates"] == [103.0, 1.0]
    assert r["features"][0]["properties"]["age_s"] == 5.0


def test_compact_columns() -> None:
    idx = LivePositions()
    idx.update_sea([sea(1, 1.0, 103.0), sea(2, 2.0, 104.0)], NOW)
    r = client(idx).get("/api/traffic/sea", params={"bbox": WORLD, "fmt": "compact"}).json()
    assert r["n"] == 2 and r["total"] == 2
    assert sorted(r["cols"]["mmsi"]) == [1, 2]
    assert len(r["cols"]["lon"]) == len(r["cols"]["lat"]) == 2


def test_delta_changed_and_removed() -> None:
    idx = LivePositions(sea_max_age_s=900)
    idx.update_sea([sea(1, 1.0, 103.0), sea(2, 2.0, 104.0)], NOW)
    c = client(idx)
    v = c.get("/api/traffic/sea", params={"bbox": WORLD}).json()["version"]
    idx.update_sea([sea(2, 2.5, 104.5)], NOW + timedelta(seconds=1))
    d = c.get("/api/traffic/sea", params={"bbox": WORLD, "since": v}).json()
    assert d["delta"] is True
    assert [f["properties"]["mmsi"] for f in d["features"]] == [2]
    # entity 1 expires (insertion clock advances beyond max age)
    idx.expire(NOW + timedelta(seconds=2000))
    d2 = c.get("/api/traffic/sea", params={"bbox": WORLD, "since": v}).json()
    assert sorted(d2["removed"]) == [1, 2]


def test_world_density_and_expiry() -> None:
    idx = LivePositions()
    idx.update_air([air("aaaaaa", 1.0, 103.0), air("bbbbbb", 1.01, 103.01, mil=True)], NOW)
    r = client(idx).get("/api/traffic/air", params={"bbox": WORLD, "zoom": 3}).json()
    assert r["type"] == "density" and r["total"] == 2
    assert sum(c["count"] for c in r["cells"]) == 2 and sum(c["mil"] for c in r["cells"]) == 1
    idx.expire(NOW + timedelta(seconds=600))
    r = client(idx).get("/api/traffic/air", params={"bbox": WORLD, "zoom": 3}).json()
    assert r["cells"] == []


def test_antimeridian_bbox() -> None:
    idx = LivePositions()
    idx.update_air(
        [air("aaaaaa", 0.0, 179.5), air("bbbbbb", 0.0, -179.5), air("cccccc", 0, 0)], NOW
    )
    r = client(idx).get("/api/traffic/air", params={"bbox": "179,-5,-179,5"}).json()
    assert {f["properties"]["hex"] for f in r["features"]} == {"aaaaaa", "bbbbbb"}


def test_store_path_has_total_and_compact() -> None:
    st = DuckDBStore(":memory:")
    st.insert_aircraft([air("aaaaaa", 1.0, 103.0)])
    app = FastAPI()
    app.include_router(traffic_router(st, now=lambda: NOW))
    c = TestClient(app)
    assert c.get("/api/traffic/air", params={"bbox": WORLD}).json()["total"] == 1
    r = c.get("/api/traffic/air", params={"bbox": WORLD, "fmt": "compact"}).json()
    assert r["cols"]["hex"] == ["aaaaaa"]


def test_bulk_insert_roundtrip_and_hot_retention() -> None:
    st = DuckDBStore(":memory:")
    st.insert_aircraft([air("aaaaaa", 1.0, 103.0, age=10), air("bbbbbb", 1.0, 103.0, age=7200)])
    st.insert_vessels([sea(1, 1.0, 103.0)])
    got = st.aircraft_window(NOW - timedelta(hours=3), NOW + timedelta(minutes=1))
    assert {o.hex for o in got} == {"aaaaaa", "bbbbbb"}
    assert st.purge_hot(NOW - timedelta(hours=1)) == 1
    assert [o.hex for o in st.aircraft_window(NOW - timedelta(hours=3), NOW)] == ["aaaaaa"]
