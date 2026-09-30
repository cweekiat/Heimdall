"""024g: aisstream global mode, Writer/lake routing of global rows, janitor, detector isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from heimdall.adapters.memory_store import MemoryStore
from heimdall.adapters.parquet_lake import ParquetLake
from heimdall.config import AOI
from heimdall.ingest.ais import AisStreamConnector
from heimdall.run import Writer
from heimdall.schemas import AircraftObs, VesselObs

AOIS = [AOI(name="malacca", lat=3.0, lon=100.8, radius_nm=250)]
T0 = "2026-09-30 12:00:00.000000000 +0000 UTC"


def _msg(mmsi: int, lat: float, lon: float, t: str) -> dict[str, Any]:
    return {
        "MessageType": "PositionReport",
        "MetaData": {"time_utc": t, "MMSI": mmsi},
        "Message": {
            "PositionReport": {"UserID": mmsi, "Latitude": lat, "Longitude": lon, "Sog": 10.0}
        },
    }


def _conn(**kw: Any) -> AisStreamConnector:
    return AisStreamConnector("aisstream", "wss://x.invalid", AOIS, "k", **kw)


def test_global_subscription_is_world_bbox() -> None:
    assert _conn(global_mode=True).subscription()["BoundingBoxes"] == [
        [[-90.0, -180.0], [90.0, 180.0]]
    ]
    assert len(_conn().subscription()["BoundingBoxes"]) >= 1  # AOI boxes when not global


def test_outside_aoi_downsampled_inside_keeps_cadence() -> None:
    c = _conn(global_mode=True, min_interval_s=10, global_min_interval_s=60)
    now = datetime.now(UTC)
    out = c._accept(_msg(111111111, 50.0, 0.0, "2026-09-30 12:00:00 +0000 UTC"), now)
    assert out is not None and out.is_global
    # 30 s later, outside AOI: thinned (60 s); 30 s later again: kept
    assert c._accept(_msg(111111111, 50.0, 0.1, "2026-09-30 12:00:30 +0000 UTC"), now) is None
    assert c._accept(_msg(111111111, 50.0, 0.2, "2026-09-30 12:01:01 +0000 UTC"), now) is not None
    # inside the AOI: 10 s cadence, not global
    a = c._accept(_msg(222222222, 3.0, 100.8, "2026-09-30 12:00:00 +0000 UTC"), now)
    b = c._accept(_msg(222222222, 3.0, 100.9, "2026-09-30 12:00:11 +0000 UTC"), now)
    assert a is not None and not a.is_global and b is not None


def _air(hex_: str, ts: datetime, glob: bool) -> AircraftObs:
    return AircraftObs(
        ts=ts, hex=hex_, provider="opensky" if glob else "adsb.lol", lat=10.0, lon=20.0,
        src_type="opensky_adsb" if glob else "adsb_icao", is_global=glob,
    )  # fmt: skip


async def test_writer_routes_global_rows_to_global_partition(tmp_path: Path) -> None:
    lake, store = ParquetLake(tmp_path), MemoryStore()
    w = Writer(bus=None, lake=lake, store=store)  # type: ignore[arg-type]
    ts = datetime.now(UTC)
    w.add([_air("aaaaaa", ts, False), _air("bbbbbb", ts, True), _air("bbbbbb", ts, True)])
    w.sea_buf.append(VesselObs(ts=ts, mmsi=111111111, provider="aisstream", lat=1, lon=2))
    w.sea_buf.append(
        VesselObs(ts=ts, mmsi=222222222, provider="aisstream", lat=1, lon=2, is_global=True)
    )
    w.flush()
    day = ts.strftime("%Y-%m-%d")
    assert (tmp_path / "aircraft" / f"date={day}").exists()
    assert (tmp_path / "global" / "aircraft" / f"date={day}").exists()
    assert (tmp_path / "vessels" / f"date={day}").exists()
    assert (tmp_path / "global" / "vessels" / f"date={day}").exists()
    assert len(store.aircraft) == 2 and w.n_global == 1  # exact (hex, ts) repeat dropped


def test_janitor_purges_only_old_global(tmp_path: Path) -> None:
    lake, store = ParquetLake(tmp_path), MemoryStore()
    old = datetime.now(UTC) - timedelta(days=10)
    new = datetime.now(UTC)
    lake.append("global/aircraft", [_air("aaaaaa", old, True), _air("bbbbbb", new, True)])
    lake.append("aircraft", [_air("cccccc", old, False)])
    store.insert_aircraft([_air("aaaaaa", old, True), _air("cccccc", old, False)])
    assert lake.purge_global(datetime.now(UTC) - timedelta(days=7)) == 1
    assert not (tmp_path / "global" / "aircraft" / f"date={old:%Y-%m-%d}").exists()
    assert (tmp_path / "global" / "aircraft" / f"date={new:%Y-%m-%d}").exists()
    assert (tmp_path / "aircraft" / f"date={old:%Y-%m-%d}").exists()  # AOI data untouched
    assert store.purge_global(datetime.now(UTC) - timedelta(days=7)) == 1
    assert [r.hex for r in store.aircraft] == ["cccccc"]
