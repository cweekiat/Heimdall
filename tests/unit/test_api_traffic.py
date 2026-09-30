"""Traffic, notices and actions-board endpoints (012a)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api import traffic
from heimdall.api.app import create_app
from heimdall.api.demo import DemoSources, LiveDemoStore, seed
from heimdall.schemas import AircraftObs, GeoEvent, VesselObs

HOSTS = ["testserver", "127.0.0.1", "localhost"]
PB = {"actions": {"watch_cell": {"label": "Watch", "risk": "high", "applies_to": ["*"]}}}
NOW = datetime.now(UTC)
BB = "0,0,10,10"


def _ac(hex_: str, dt: float, lat: float = 5, lon: float = 5, **kw: Any) -> AircraftObs:
    return AircraftObs(
        ts=NOW - timedelta(seconds=dt),
        hex=hex_,
        provider=kw.pop("provider", "adsb.lol"),
        lat=lat,
        lon=lon,
        src_type="adsb_icao",
        **kw,
    )


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    s.insert_aircraft(
        [
            _ac("abc001", 30, lat=1, lon=1),
            _ac("abc001", 5, lat=2, lon=2, nic=8, nac_p=9),  # latest fix wins
            _ac("abc002", 10, nic=4, nac_p=9),  # NIC<7 -> gps_bad
            _ac("abc003", 10, nac_p=7),  # NACp<8 -> gps_bad
            _ac("abc004", 10, provider="adsb.fi"),
            _ac("abc005", 10, lat=50, lon=50),  # outside bbox
            _ac("abc006", 300),  # too old
        ]
    )
    s.insert_vessels(
        [
            VesselObs(ts=NOW - timedelta(seconds=90), mmsi=1, provider="aisstream", lat=3, lon=3),
            VesselObs(
                ts=NOW - timedelta(seconds=10),
                mmsi=1,
                provider="aisstream",
                lat=4,
                lon=4,
                ship_type=71,
                sog=9.0,
            ),
            VesselObs(ts=NOW - timedelta(seconds=10), mmsi=2, provider="gfw", lat=4, lon=4),
        ]
    )
    return s


@pytest.fixture
def client(store: MemoryStore) -> TestClient:
    return TestClient(create_app(store, playbook=PB, allowed_hosts=HOSTS))


def _props(r: Any) -> dict[str, dict[str, Any]]:
    return {f["properties"]["hex"]: f["properties"] for f in r.json()["features"]}


def test_air_latest_bbox_age_and_flags(client: TestClient) -> None:
    r = client.get(f"/api/traffic/air?bbox={BB}")
    assert r.status_code == 200
    p = _props(r)
    assert set(p) == {"abc001", "abc002", "abc003", "abc004"}
    feats = {f["properties"]["hex"]: f for f in r.json()["features"]}
    assert feats["abc001"]["geometry"]["coordinates"] == [2, 2]
    assert p["abc001"]["gps_bad"] is False
    assert p["abc002"]["gps_bad"] is True and p["abc003"]["gps_bad"] is True
    assert 4 <= p["abc001"]["age_s"] <= 8
    ages = [f["properties"]["age_s"] for f in r.json()["features"]]
    assert ages == sorted(ages)  # newest first
    assert "abc006" in _props(client.get(f"/api/traffic/air?bbox={BB}&max_age_s=600"))


def test_air_source_filter_and_bad_input(client: TestClient) -> None:
    assert set(_props(client.get(f"/api/traffic/air?bbox={BB}&sources=adsb_fi"))) == {"abc004"}
    both = client.get(f"/api/traffic/air?bbox={BB}&sources=adsb_lol,adsb_fi")
    assert set(_props(both)) == {"abc001", "abc002", "abc003", "abc004"}
    for bad in ["", "1,2,3", "a,b,c,d", "0,10,10,0", "0,0,10,95", "nan,0,1,1"]:
        assert client.get("/api/traffic/air", params={"bbox": bad}).status_code == 422
    assert client.get("/api/traffic/air").status_code == 422
    assert client.get(f"/api/traffic/air?bbox={BB}&sources=a;b").status_code == 422
    assert client.get(f"/api/traffic/air?bbox={BB}&max_age_s=0").status_code == 422


def test_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(traffic, "CAP", 3)
    s = MemoryStore()
    s.insert_aircraft([_ac(f"c{i:05x}", i + 1) for i in range(10)])
    c = TestClient(create_app(s, playbook=PB, allowed_hosts=HOSTS))
    d = c.get(f"/api/traffic/air?bbox={BB}").json()
    assert [f["properties"]["hex"] for f in d["features"]] == ["c00000", "c00001", "c00002"]
    assert d["truncated"] is True


def test_sea_latest_class_and_source(client: TestClient) -> None:
    d = client.get(f"/api/traffic/sea?bbox={BB}").json()
    by = {f["properties"]["mmsi"]: f["properties"] for f in d["features"]}
    assert set(by) == {1, 2}
    assert by[1]["ship_class"] == "cargo" and by[1]["sog"] == 9.0
    only = client.get(f"/api/traffic/sea?bbox={BB}&sources=aisstream").json()["features"]
    assert [f["properties"]["mmsi"] for f in only] == [1]
    assert traffic.ship_class(80) == "tanker" and traffic.ship_class(None) == "other"


def test_trails(client: TestClient) -> None:
    d = client.get("/api/traffic/air/abc001/trail?min=5").json()
    assert d["geometry"]["type"] == "LineString"
    assert d["geometry"]["coordinates"] == [[1, 1], [2, 2]]
    assert client.get("/api/traffic/air/abc999/trail").status_code == 404
    assert client.get("/api/traffic/air/zzzzzz/trail").status_code == 422
    s = client.get("/api/traffic/sea/1/trail?min=10").json()
    assert s["geometry"]["coordinates"] == [[3, 3], [4, 4]]


def test_in_alert_flag_from_demo() -> None:
    c = TestClient(create_app(seed(LiveDemoStore()), playbook=PB, allowed_hosts=HOSTS))
    d = c.get("/api/traffic/air?bbox=-180,-90,180,90&max_age_s=30").json()
    props = [f["properties"] for f in d["features"]]
    assert len(props) >= 300
    assert sum(p["in_alert"] for p in props) == 8
    assert all(p["gps_bad"] for p in props if p["in_alert"])
    assert any(p["mil"] for p in props)
    sea = c.get("/api/traffic/sea?bbox=-180,-90,180,90&max_age_s=30").json()["features"]
    assert len(sea) >= 140


def test_notices_active_and_untrusted() -> None:
    c = TestClient(create_app(seed(LiveDemoStore()), playbook=PB, allowed_hosts=HOSTS))
    d = c.get("/api/notices").json()
    assert len(d["features"]) == 3
    assert all(f["properties"]["untrusted"] is True for f in d["features"])
    assert any("IGNORE PREVIOUS" in f["properties"]["text"] for f in d["features"])
    scs = c.get("/api/notices?aoi=south_china_sea").json()["features"]
    assert [f["properties"]["ref"] for f in scs] == ["HYDROPAC 41/26"]
    assert c.get("/api/notices?aoi=nowhere").status_code == 404
    past = (NOW - timedelta(days=10)).isoformat()
    assert c.get("/api/notices", params={"at": past}).json()["features"] == []


def test_notices_exclude_cancelled_and_expired() -> None:
    s = MemoryStore()
    base: dict[str, Any] = {
        "ts": NOW - timedelta(hours=1),
        "source": "x",
        "domain": "sea",
        "kind": "nav_warning",
        "lat": 1,
        "lon": 1,
    }
    s.insert_events(
        [
            GeoEvent(id="a", attrs={"status": "cancelled"}, **base),
            GeoEvent(id="b", valid_to=NOW - timedelta(minutes=1), **base),
            GeoEvent(id="c", **base),
        ]
    )
    c = TestClient(create_app(s, playbook=PB, allowed_hosts=HOSTS))
    assert [f["properties"]["id"] for f in c.get("/api/notices").json()["features"]] == ["c"]


def test_actions_board_and_sources_demo() -> None:
    c = TestClient(
        create_app(seed(MemoryStore()), playbook=PB, allowed_hosts=HOSTS, sources=DemoSources()),
        headers={"Origin": "http://testserver"},
    )
    d = c.get("/api/actions").json()
    assert {a["status"] for a in d} == {"proposed", "approved", "done", "rejected"}
    assert d[0]["label"] == "Watch" and d[0]["risk"] == "high" and d[0]["alert"]["title"]
    assert {a["status"] for a in c.get("/api/actions?status=proposed").json()} == {"proposed"}
    st = {s["key"]: s for s in c.get("/api/sources").json()}
    assert {s["status"] for s in st.values()} >= {"ok", "degraded", "needs_key", "off"}
    on = c.post("/api/sources/china_msa", json={"enabled": True}).json()
    assert on["enabled"] and on["status"] == "ok"
    assert c.post("/api/sources/nope", json={"enabled": True}).status_code == 404
