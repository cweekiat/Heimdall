"""031 global GNSS map: ADS-B world sweep interleaved with AOI polls, empty points backed off."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from heimdall.config import AOI
from heimdall.ingest.adsb import AdsbAggregatorConnector, sweep_grid
from heimdall.ingest.registry import _sweep_kw
from heimdall.schemas import AircraftObs

T = datetime(2026, 10, 1, tzinfo=UTC)


def _conn(sweep: list[AOI]) -> tuple[AdsbAggregatorConnector, list[str]]:
    c = AdsbAggregatorConnector(
        "adsb_fi",
        "https://x.invalid",
        "/{lat}/{lon}/{radius}",
        [AOI(name="a", lat=1, lon=1, radius_nm=250)],
        sweep=sweep,
        sweep_every=2,
        sweep_backoff=3,
    )
    calls: list[str] = []

    async def fetch(aoi: AOI) -> tuple[list[AircraftObs], int]:
        calls.append(aoi.name)
        if aoi.name == "sweep:1":  # empty ocean point
            return [], 0
        return [
            AircraftObs(
                ts=T,
                hex="abc123",
                provider="adsb.fi",
                lat=aoi.lat,
                lon=aoi.lon,
                src_type="adsb_icao",
                nic=8,
            )
        ], 0

    c.fetch_aoi = fetch  # type: ignore[method-assign]
    return c, calls


async def test_sweep_interleaves_marks_global_and_backs_off() -> None:
    pts = [AOI(name=f"sweep:{i}", lat=10.0 * i, lon=0, radius_nm=250) for i in range(3)]
    c, calls = _conn(pts)
    out: list[list[AircraftObs]] = [await c.poll_once() for _ in range(18)]
    assert calls[:3] == ["a", "a", "sweep:0"]  # one sweep point after every 2 AOI polls
    sweep_rows = [o for batch in out for o in batch if o.is_global]
    aoi_rows = [o for batch in out for o in batch if not o.is_global]
    assert sweep_rows and aoi_rows and all(o.lat == 1 for o in aoi_rows)
    assert calls.count("sweep:1") == 1  # empty point skipped for the next passes
    assert calls.count("sweep:0") >= 2


def test_grid_and_config() -> None:
    g = sweep_grid()
    assert 800 < len(g) < 1400 and all(-50 <= p.lat <= 70 and -180 <= p.lon < 180 for p in g)
    assert _sweep_kw(None) == {} and _sweep_kw({"enabled": False}) == {}
    kw: dict[str, Any] = _sweep_kw({"enabled": True, "every": 3})
    assert kw["sweep_every"] == 3 and len(kw["sweep"]) == len(g)
