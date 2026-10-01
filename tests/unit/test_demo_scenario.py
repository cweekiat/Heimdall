"""Task 040: real-data demo scenario (replay seek, scenario file, military-only archive filter)."""

from __future__ import annotations

import gzip
import io
import json
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from heimdall.api.app import create_app
from heimdall.api.demo import ReplayDemoStore, add_seek_route, load_scenario, replay_view
from heimdall.schemas import AircraftObs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from filter_adsblol import filter_sources

T0 = datetime(2025, 6, 18, tzinfo=UTC)
ORIGIN = {"Origin": "http://127.0.0.1"}


def _obs(t: datetime, hex_: str = "ae0001") -> AircraftObs:
    return AircraftObs(
        ts=t,
        hex=hex_,
        provider="adsb.lol",
        lat=50.0,
        lon=5.0,
        src_type="adsb_icao",
        mil=True,
        type_code="K35R",
        alt_baro=30000,
    )


def test_seek_maps_wall_clock_onto_chosen_replay_time() -> None:
    store = ReplayDemoStore([_obs(T0 + timedelta(hours=h)) for h in range(48)])
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    at = store.seek(T0 + timedelta(hours=30), now)
    assert at == T0 + timedelta(hours=30)
    assert store.replay_now(now) == T0 + timedelta(hours=30)
    rows = store.aircraft_window(now - timedelta(minutes=30), now + timedelta(minutes=1))
    assert [r.ts for r in rows] == [now]  # the 30 h row, shifted onto the wall clock
    assert store.seek(T0 - timedelta(days=9), now) == T0  # clamped to the recording


def test_scenario_file_reaches_ui_config_and_seek_route(tmp_path: Path) -> None:
    p = tmp_path / "demo.parquet"
    sc = {
        "title": "t",
        "summary": "s",
        "caveat": "c",
        "sources": "x",
        "steps": [{"at": "2025-06-18T06:00:00Z"}],
    }
    p.with_name("demo.scenario.json").write_text(json.dumps(sc))
    assert load_scenario(p) == sc and load_scenario(tmp_path / "none.parquet") is None
    store = ReplayDemoStore([_obs(T0 + timedelta(hours=h)) for h in range(24)])
    view = replay_view(store, p)
    assert view["scenario"] == sc and "banner" in view
    app = create_app(store, ui_extra=view)
    add_seek_route(app, store)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/api/ui-config").json()["scenario"]["title"] == "t"
    r = c.post("/api/replay/seek", json={"at": "2025-06-18T06:00:00Z"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["at"].startswith("2025-06-18T06:00:00")
    assert c.post("/api/replay/seek", json={"at": "soon"}, headers=ORIGIN).status_code == 422


def test_filter_keeps_only_military_and_thins(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for i, (hx, flags) in enumerate([("ae0001", 1), ("a00001", 0), ("43c000", 3)]):
            rows = [[dt, 50.0, 5.0, 30000, 450, 90, 0, 0, None] for dt in (0, 30, 90)]
            d = {"icao": hx, "t": "K35R", "timestamp": T0.timestamp(), "trace": rows}
            if flags:
                d["dbFlags"] = flags
            raw = gzip.compress(json.dumps(d).encode())
            ti = tarfile.TarInfo(f"traces/0{i}/trace_full_{hx}.json")
            ti.size = len(raw)
            tar.addfile(ti, io.BytesIO(raw))
    p = tmp_path / "day.tar"
    p.write_bytes(buf.getvalue())
    obs = filter_sources([p], mil_only=True, every_s=60)
    assert sorted({o.hex for o in obs}) == ["43c000", "ae0001"] and all(o.mil for o in obs)
    assert len([o for o in obs if o.hex == "ae0001"]) == 2  # rows at 0 s and 90 s; 30 s thinned
