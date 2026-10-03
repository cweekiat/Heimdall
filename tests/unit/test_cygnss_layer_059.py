"""059: CYGNSS L1 noise day map API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import h3
from fastapi import FastAPI
from fastapi.testclient import TestClient

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.cygnss import cygnss_router
from heimdall.schemas import Domain, GeoEvent


def _ev(day: datetime, lat: float, lon: float, db: float, **attrs: object) -> GeoEvent:
    cell = h3.latlng_to_cell(lat, lon, 3)
    a: dict[str, object] = {"h3": cell, "n": 80, "n_sats": 2, "ocean_frac": 1.0, "day": "d"}
    a.update(attrs)
    return GeoEvent(
        id=f"cygnss|{day.date()}|{cell}",
        ts=day,
        valid_from=day,
        valid_to=day + timedelta(days=1),
        source="cygnss",
        domain=Domain.SPECTRUM,
        kind="l1_noise",
        lat=lat,
        lon=lon,
        value=db,
        attrs=a,
    )


def test_day_map_filters_quiet_cells_and_flags_rules() -> None:
    d1 = (datetime.now(UTC) - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    d2 = d1 + timedelta(days=1)
    st = MemoryStore()
    st.insert_events(
        [
            _ev(d1, 5.0, 110.0, 3.0),  # older day
            _ev(d2, 5.0, 110.0, 0.1),  # quiet: omitted
            _ev(d2, 6.0, 111.0, 2.8),  # open ocean >= 2.5: level
            _ev(d2, 7.0, 112.0, 3.0, ocean_frac=0.0),  # land < 3.5: shown, not flagged
            _ev(d2, 7.5, 112.5, 1.5, ocean_frac=0.2),  # warm coast below the land floor: omitted
            _ev(d2, 8.0, 113.0, 1.6, ocean_frac=0.0, base_db=-0.1, base_mad_db=0.05, base_days=14),
            _ev(d2, 9.0, 114.0, 2.9, n=5),  # too few samples: shown, never flagged
        ]
    )
    app = FastAPI()
    app.include_router(cygnss_router(st))
    c = TestClient(app)
    assert c.get("/api/cygnss/days").json()["days"] == [str(d1.date()), str(d2.date())]
    d = c.get("/api/cygnss/day").json()
    assert d["date"] == str(d2.date()) and d["total_cells"] == 6 and d["shown_cells"] == 4
    flags = sorted((x["excess_db"], x["flag"]) for x in d["cells"])
    assert flags == [(1.6, "change"), (2.8, "level"), (2.9, None), (3.0, None)]
    assert c.get(f"/api/cygnss/day?date={d1.date()}").json()["cells"][0]["flag"] == "level"
    assert c.get("/api/cygnss/day?date=2020-01-01").status_code == 404
    assert c.get("/api/cygnss/day?date=x;drop").status_code == 422
