"""015: user-drawn AOIs - API validation, persistence round-trip, runtime + connectors (offline)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heimdall import config
from heimdall.adapters.aoi_file import FileAoiStore
from heimdall.adapters.memory_store import MemoryStore
from heimdall.aoi_ctl import AoiService
from heimdall.api.app import create_app
from heimdall.ingest.adsb import AdsbAggregatorConnector
from heimdall.ingest.ais import AisStreamConnector
from heimdall.ingest.registry import build_connectors

HOSTS = ["testserver", "127.0.0.1", "localhost"]
LOCAL = {"Origin": "http://testserver"}
CIRCLE = {"name": "Harbour Watch", "role": "primary", "shape": "circle",
          "lat": 1.2, "lon": 103.8, "radius_nm": 40}  # fmt: skip
POLY = {"name": "Box", "shape": "polygon",
        "polygon": [[10.0, 110.0], [10.0, 112.0], [12.0, 112.0], [12.0, 110.0]]}  # fmt: skip


class FakeSources:
    def __init__(self) -> None:
        self.restarted: list[str] = []

    def list(self) -> list[Any]:
        return []

    async def set_enabled(self, key: str, enabled: bool) -> Any:
        raise KeyError(key)

    async def restart(self, key: str) -> None:
        self.restarted.append(key)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, FakeSources, Path]]:
    s = config.settings()
    before = list(s.aois)
    src = FakeSources()
    path = tmp_path / "state" / "aois.json"
    svc = AoiService(
        FileAoiStore(path), s, src, {"adsb_lol": {"uses_aois": True}, "gfw": {"uses_aois": True}}
    )
    app = create_app(MemoryStore(), playbook={}, allowed_hosts=HOSTS, aoi_service=svc)
    yield TestClient(app, client=("127.0.0.1", 5000)), src, path
    s.aois = before


def test_create_list_patch_delete_and_runtime(env: tuple[TestClient, FakeSources, Path]) -> None:
    c, src, path = env
    r = c.post("/api/aois/user", json=CIRCLE, headers=LOCAL)
    assert r.status_code == 201
    uid = r.json()["id"]
    assert src.restarted == ["adsb_lol", "gfw"]
    rt = [a for a in c.get("/api/aois").json() if a["origin"] == "user"]
    assert rt[0]["name"] == uid and rt[0]["label"] == "Harbour Watch"
    assert path.exists()
    r = c.patch(
        f"/api/aois/user/{uid}", json={"name": "Renamed_1", "role": "control"}, headers=LOCAL
    )
    assert r.status_code == 200 and r.json()["role"] == "control"
    c.patch(f"/api/aois/user/{uid}", json={"active": False}, headers=LOCAL)
    assert not [a for a in c.get("/api/aois").json() if a["origin"] == "user"]
    assert c.get("/api/aois/user").json()[0]["active"] is False  # still listed, not monitored
    assert c.delete(f"/api/aois/user/{uid}", headers=LOCAL).status_code == 200
    assert c.get("/api/aois/user").json() == []
    assert c.delete(f"/api/aois/user/{uid}", headers=LOCAL).status_code == 404


def test_polygon_becomes_bounding_circle(env: tuple[TestClient, FakeSources, Path]) -> None:
    c, _, _ = env
    r = c.post("/api/aois/user", json=POLY, headers=LOCAL)
    assert r.status_code == 201
    a = r.json()
    assert (a["lat"], a["lon"]) == (11.0, 111.0) and a["radius_nm"] > 60
    assert len(a["polygon"]) == 4
    rt = next(x for x in config.settings().aois if x.name == a["id"])
    assert rt.contains(11.0, 111.0) and not rt.contains(13.0, 111.0)
    # inside bounding circle but outside the polygon: a concave notch
    ring = [(0, 0), (0, 4), (2, 4), (2, 2), (4, 2), (4, 0)]
    notch = config.AOI(name="n", lat=2, lon=2, radius_nm=400, polygon=ring)
    assert notch.contains(1, 1) and not notch.contains(3.5, 3.5)


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "x" * 41},
        {"name": "bad;name"},
        {"name": "<b>x</b>"},
        {"name": ""},
        {"radius_nm": 4},
        {"radius_nm": 501},
        {"lat": 91},
        {"lon": 181},
        {"evil": 1},
        {"shape": "square"},
    ],
)
def test_circle_validation(
    env: tuple[TestClient, FakeSources, Path], patch: dict[str, Any]
) -> None:
    c, src, _ = env
    r = c.post("/api/aois/user", json={**CIRCLE, **patch}, headers=LOCAL)
    assert r.status_code == 422
    for v in patch.values():
        assert not str(v) or str(v) not in r.text  # never echoed
    assert src.restarted == []


@pytest.mark.parametrize(
    "ring",
    [
        [[1, 1], [1, 2]],  # too few
        [[1, 1], [2, 2], [3, 3]],  # degenerate
        [[0, 0], [0, 11], [1, 11]],  # span > 10 deg
        [[0, 0], [95, 1], [1, 2]],  # bad latitude
        [[0, 0], [1, 181], [2, 1]],  # bad longitude
        [[i / 100, (i % 7) / 100] for i in range(201)],  # too many
    ],
)
def test_polygon_validation(env: tuple[TestClient, FakeSources, Path], ring: list[Any]) -> None:
    c, _, _ = env
    r = c.post(
        "/api/aois/user", json={"name": "P", "shape": "polygon", "polygon": ring}, headers=LOCAL
    )
    assert r.status_code == 422 and "polygon" not in r.text.lower().replace("invalid area", "")


def test_mixed_shape_fields_rejected(env: tuple[TestClient, FakeSources, Path]) -> None:
    c, _, _ = env
    both = {**CIRCLE, "polygon": POLY["polygon"]}
    assert c.post("/api/aois/user", json=both, headers=LOCAL).status_code == 422
    partial = {"name": "C", "shape": "circle", "lat": 1, "lon": 1}
    assert c.post("/api/aois/user", json=partial, headers=LOCAL).status_code == 422


def test_nan_rejected(env: tuple[TestClient, FakeSources, Path]) -> None:
    c, _, _ = env
    raw = '{"name":"N","shape":"circle","lat":1,"lon":1,"radius_nm":NaN}'
    h = {**LOCAL, "Content-Type": "application/json"}
    assert c.post("/api/aois/user", content=raw, headers=h).status_code == 422


def test_limit_20(env: tuple[TestClient, FakeSources, Path]) -> None:
    c, _, _ = env
    for _ in range(20):
        assert c.post("/api/aois/user", json=CIRCLE, headers=LOCAL).status_code == 201
    assert c.post("/api/aois/user", json=CIRCLE, headers=LOCAL).status_code == 409


def test_patch_rejects_unknown_and_geometry(env: tuple[TestClient, FakeSources, Path]) -> None:
    c, _, _ = env
    uid = c.post("/api/aois/user", json=CIRCLE, headers=LOCAL).json()["id"]
    for bad in ({"lat": 5}, {"name": "a/b"}, {"role": "boss"}, {"id": "x"}):
        assert c.patch(f"/api/aois/user/{uid}", json=bad, headers=LOCAL).status_code == 422


def test_origin_required_for_mutations(env: tuple[TestClient, FakeSources, Path]) -> None:
    c, src, _ = env
    assert c.post("/api/aois/user", json=CIRCLE).status_code == 403
    evil = {"Origin": "http://evil.example"}
    assert c.post("/api/aois/user", json=CIRCLE, headers=evil).status_code == 403
    assert c.delete("/api/aois/user/user_abc", headers=evil).status_code == 403
    assert c.get("/api/aois/user").json() == [] and src.restarted == []


def test_persistence_round_trip(tmp_path: Path) -> None:
    import asyncio

    from heimdall.schemas import UserAoiIn

    path = tmp_path / "aois.json"
    s = config.Settings(aois=[config.AOI(name="cfg", lat=0, lon=0, radius_nm=10)])
    svc = AoiService(FileAoiStore(path), s)
    u = asyncio.run(svc.create(UserAoiIn.model_validate(POLY)))
    s2 = config.Settings(aois=[config.AOI(name="cfg", lat=0, lon=0, radius_nm=10)])
    svc2 = AoiService(FileAoiStore(path), s2)
    assert svc2.list() == [u]
    assert [a.name for a in s2.aois] == ["cfg", u.id]  # config AOI kept, user AOI appended
    assert s2.aois[1].polygon == [tuple(p) for p in POLY["polygon"]]  # type: ignore[attr-defined]
    path.write_text("{not json")
    assert FileAoiStore(path).load() == []  # corrupt file never crashes startup


def test_connectors_see_new_aoi(tmp_path: Path) -> None:
    import asyncio

    from heimdall.schemas import UserAoiIn

    s = config.Settings(aois=[config.AOI(name="cfg", lat=0, lon=0, radius_nm=10)])
    svc = AoiService(FileAoiStore(tmp_path / "a.json"), s)
    asyncio.run(svc.create(UserAoiIn.model_validate(CIRCLE)))
    src = {
        "sources": {
            k: v for k, v in config.sources()["sources"].items() if k in ("adsb_lol", "aisstream")
        }
    }
    built = {c.name: c for c in build_connectors(s, src)}
    adsb = built["adsb_lol"]
    assert isinstance(adsb, AdsbAggregatorConnector)
    assert [a.label for a in adsb.aois] == [None, "Harbour Watch"]
    ais = built["aisstream"]
    assert isinstance(ais, AisStreamConnector)
    boxes = ais.subscription()["BoundingBoxes"]
    assert len(boxes) == 2 and boxes[1][0][0] < 1.2 < boxes[1][1][0]
