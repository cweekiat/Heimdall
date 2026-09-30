"""031: GPSJam-style GNSS interference map (pure core, lake adapter parity, API)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from heimdall.adapters.gnss_lake import GnssLake
from heimdall.adapters.memory_store import MemoryStore
from heimdall.adapters.parquet_lake import ParquetLake
from heimdall.api.app import create_app
from heimdall.geo import h3_cell
from heimdall.gnss_map import AcCount, aggregate, compare, counts_from_obs, gpsjam_class
from heimdall.gnss_service import LakeGnssService, StoreGnssService
from heimdall.schemas import AircraftObs

T0 = datetime(2026, 9, 30, 6, tzinfo=UTC)
CELL = h3_cell(1.3, 103.8, 4)


def ac(
    hex_: str, nic: int | None, *, mil: bool = False, dt: int = 0, lat: float = 1.3
) -> AircraftObs:
    return AircraftObs(
        ts=T0 + timedelta(seconds=dt),
        hex=hex_,
        provider="adsb.fi",
        lat=lat,
        lon=103.8,
        src_type="adsb_icao",
        nic=nic,
        nac_p=None if nic is None else 9,
        mil=mil,
        h3_r4=h3_cell(lat, 103.8, 4),
        h3_r6=h3_cell(lat, 103.8, 6),
    )


def test_gpsjam_formula_and_classes() -> None:
    # 20 aircraft, 3 bad -> 100 * (3 - 1) / 20 = 10 % -> yellow (<= 10)
    counts = [AcCount(CELL, f"{i:06x}", 4, 4 if i < 3 else 0) for i in range(20)]
    [c] = aggregate(counts)
    assert (c.good, c.bad, c.total, c.pct, c.cls) == (17, 3, 20, 10.0, "yellow")
    assert gpsjam_class(1.99) == "green" and gpsjam_class(10.01) == "red"


def test_single_bad_transponder_is_denoised() -> None:
    [c] = aggregate([AcCount(CELL, "a", 5, 5), AcCount(CELL, "b", 5, 0)])
    assert c.bad == 1 and c.pct == 0.0 and c.cls == "green"


def test_military_excluded_and_dwell() -> None:
    counts = [
        AcCount(CELL, "m1", 4, 4, mil=True),
        AcCount(CELL, "b1", 4, 4),
        AcCount(CELL, "b2", 4, 4),
        AcCount(CELL, "p", 4, 1),  # 25 % of reports degraded: below dwell 0.5 -> good
        AcCount(CELL, "g", 4, 0),
    ]
    [c] = aggregate(counts)
    assert c.total == 4 and c.bad == 2 and c.mil_excluded == 1 and c.good == 2
    [c2] = aggregate(counts, exclude_military=False)
    assert c2.total == 5 and c2.bad == 3


def test_no_data_cells_absent_and_unknown_reports_ignored() -> None:
    obs = [ac("aaaaaa", None), ac("bbbbbb", 3, lat=10.0)]
    cells = aggregate(counts_from_obs(obs))
    assert [c.h3 for c in cells] == [h3_cell(10.0, 103.8, 4)]


def test_lake_sql_matches_python_path(tmp_path: Path) -> None:
    obs = [ac(f"{i:06x}", 3 if i % 3 == 0 else 9, dt=i, mil=i == 3) for i in range(12)]
    obs += [ac("dddddd", None, dt=50)]
    ParquetLake(tmp_path).append("aircraft", obs)
    lake = GnssLake(tmp_path)
    sql = aggregate(lake.counts(T0 - timedelta(hours=1), T0 + timedelta(hours=1)))
    py = aggregate(counts_from_obs(obs))
    assert [(c.h3, c.good, c.bad, c.total, c.mil_excluded) for c in sql] == [
        (c.h3, c.good, c.bad, c.total, c.mil_excluded) for c in py
    ]
    assert lake.hours_with_data(T0 - timedelta(hours=1), T0 + timedelta(hours=1)) == 1


def test_finished_day_written_once_and_read_back(tmp_path: Path) -> None:
    ParquetLake(tmp_path).append("aircraft", [ac(f"{i:06x}", 3, dt=i) for i in range(6)])
    svc = LakeGnssService(GnssLake(tmp_path), {}, ["adsb.fi"])
    svc.refresh(T0 + timedelta(days=1))
    d = svc.day("2026-09-30")
    assert d is not None and d.incomplete and d.hours_with_data == 1 and d.cells[0].cls == "red"
    assert svc.days() == ["2026-09-30"] and svc.day("last24h") is not None


def test_compare_with_gpsjam_rows() -> None:
    [ours] = aggregate([AcCount(CELL, f"{i:06x}", 1, int(i < 4)) for i in range(10)])  # 30 % red
    res = compare([ours], {CELL: (6, 4)})
    assert res.n_both == 1 and res.agreement == 1.0
    assert compare([ours], {CELL: (1, 1)}).n_both == 0  # too few aircraft on their side


def test_api_day_and_days() -> None:
    store = MemoryStore()
    now = datetime.now(UTC)
    store.insert_aircraft(
        [ac(f"{i:06x}", 3 if i < 4 else 9).model_copy(update={"ts": now}) for i in range(10)]
    )
    app = create_app(store, gnss=StoreGnssService(store, {}))
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    r = c.get("/api/gnss/day", params={"date": "last24h"}).json()
    assert r["date"] == "last24h" and r["cells"][0]["cls"] == "red" and "method" in r
    assert (
        c.get("/api/gnss/day", params={"date": "last24h", "bbox": "0,0,1,1"}).json()["cells"] == []
    )
    assert c.get("/api/gnss/day", params={"date": "2026-09-30"}).status_code == 404
    assert c.get("/api/gnss/day", params={"date": "../etc"}).status_code == 422
    assert c.get("/api/gnss/days").json() == {"days": [], "last24h": True}
