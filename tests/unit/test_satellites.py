from __future__ import annotations

from datetime import UTC, datetime

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.demo import _seed_orbits
from heimdall.api.satellites import satellite_positions
from heimdall.geo import haversine_km

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_positions_and_tracks_from_stored_elements() -> None:
    st = MemoryStore()
    _seed_orbits(st, NOW)
    sats = satellite_positions(st, NOW, past_min=10, next_min=20)
    assert len(sats) == 18  # 5 Earth-observation + 12 GNSS + 1 station (demo)
    s = next(x for x in sats if x["norad_id"] == 39634)
    assert -90 <= s["lat"] <= 90 and -180 <= s["lon"] <= 180
    assert 600 < s["alt_km"] < 900  # LEO sun-synchronous
    gnss = next(x for x in sats if x["group"] == "GNSS")
    assert 19000 < gnss["alt_km"] < 21500  # MEO
    assert len(s["past"]) == 11 and len(s["next"]) == 21
    lon, lat, alt = s["past"][-1]
    assert abs(alt - s["alt_km"]) < 1
    assert haversine_km(lat, lon, s["lat"], s["lon"]) < 1  # track passes through "now"
    lon1, lat1, _ = s["next"][1]
    assert 350 < haversine_km(lat, lon, lat1, lon1) < 500  # ~7 km/s ground speed, 60 s step


def test_no_elements_no_satellites() -> None:
    assert satellite_positions(MemoryStore(), NOW, 10, 10) == []


def test_group_label_for_unconfigured_satellites() -> None:
    st = MemoryStore()
    _seed_orbits(st, NOW)
    e = st.events[0].model_copy(
        update={"id": "x", "attrs": {**st.events[0].attrs, "norad_id": 99999, "group": "GNSS"}}
    )
    st.insert_events([e])
    groups = {s["norad_id"]: s["group"] for s in satellite_positions(st, NOW, 5, 5)}
    assert groups[99999] == "GNSS" and groups[39634] == "Earth observation"
