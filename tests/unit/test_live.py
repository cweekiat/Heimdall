"""Global (OpenSky) snapshot rows stay visible between polls; AOI rows keep the short window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from heimdall.live import GLOBAL_AIR_MAX_AGE_S, LivePositions
from heimdall.schemas import AircraftObs

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
WORLD = (-180.0, -85.0, 180.0, 85.0)


def obs(hex_: str, age: float, *, is_global: bool) -> AircraftObs:
    return AircraftObs(
        ts=NOW - timedelta(seconds=age),
        hex=hex_,
        provider="opensky" if is_global else "adsb.lol",
        lat=1.3,
        lon=103.8,
        src_type="adsb_icao",
        is_global=is_global,
    )


def test_global_rows_survive_a_slow_poll_but_aoi_rows_do_not() -> None:
    live = LivePositions()
    live.update_air([obs("aaaaaa", 150, is_global=True), obs("bbbbbb", 150, is_global=False)], NOW)
    got = {e.key for e in live.air.query(WORLD, NOW, 60)}
    assert got == {"aaaaaa"}


def test_global_rows_expire_after_their_window() -> None:
    live = LivePositions()
    live.update_air([obs("aaaaaa", 0, is_global=True)], NOW)
    live.expire(NOW + timedelta(seconds=GLOBAL_AIR_MAX_AGE_S + 60))
    assert live.air.query(WORLD, NOW + timedelta(seconds=GLOBAL_AIR_MAX_AGE_S + 60), 60) == []
