"""047: aircraft investigation - airfields, take-off, activity rules, type reference, endpoint."""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from heimdall import milactivity as ma
from heimdall import miltypes
from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.ingest.ourairports import AirfieldIndex, parse_csv
from heimdall.schemas import AircraftObs

CSV = (Path(__file__).parents[1] / "fixtures" / "ourairports" / "airports.csv").read_text()
T0 = datetime(2026, 10, 3, 6, 0, tzinfo=UTC).timestamp()


def idx() -> AirfieldIndex:
    return AirfieldIndex(parse_csv(CSV))


def test_airfields_parse_and_nearest_prefers_the_base_over_its_heliport() -> None:
    fields = parse_csv(CSV)
    assert {f.ident for f in fields} == {"EGUL", "GB-H1", "EDDF"}  # closed dropped
    assert next(f for f in fields if f.ident == "EGUL").military
    hit = idx().nearest(52.410, 0.564, 15.0, True)
    assert hit is not None and hit[0].ident == "EGUL"
    assert idx().nearest(40.0, 0.0, 15.0, True) is None


def _line(lat0: float, lon0: float, brg: float, n: int, step_km: float, alt: float, t0: float):
    out = []
    for i in range(n):
        d = i * step_km / 111.0
        out.append(
            (
                t0 + i * 300,
                lat0 + d * math.cos(math.radians(brg)),
                lon0 + d * math.sin(math.radians(brg)) / math.cos(math.radians(lat0)),
                alt,
                420.0,
            )
        )
    return out


def test_departure_took_off_first_seen_and_on_ground() -> None:
    ground = [(T0, 52.409, 0.561, -1.0, 0.0)]
    climb = [(T0 + 300, 52.45, 0.70, 2500.0, 250.0), (T0 + 600, 52.6, 1.2, 15000.0, 380.0)]
    d = ma.departure(ground + climb, idx().nearest)
    assert d["kind"] == "took_off" and d["airfield"]["ident"] == "EGUL"
    assert "RAF Lakenheath" in d["text"]
    # a 2-hour gap, then seen high over the sea: take-off not covered
    late = [(T0 + 8000, 55.0, 3.0, 30000.0, 450.0), (T0 + 8300, 55.2, 3.5, 30000.0, 450.0)]
    assert ma.departure(ground + climb + late, idx().nearest)["kind"] == "first_seen"
    assert ma.departure(ground, idx().nearest)["kind"] == "on_ground"


def test_racetrack_tanker_is_a_refuelling_track() -> None:
    pts = []
    for i in range(10):  # 45 min racetrack: out and back legs of ~60 km
        a = math.radians(i * 80)
        pts.append(
            (T0 + i * 300, 54.0 + 0.25 * math.sin(a), 2.0 + 0.5 * math.cos(a), 26000.0, 420.0)
        )
    act = ma.activity(pts, "tanker", pts[-1][0], idx().nearest)
    assert act.label == "Likely air-refuelling track" and act.confidence == "likely"
    assert act.reasons and "turning" in act.reasons[0]
    assert ma.activity(pts, "airlift", pts[-1][0], idx().nearest).confidence == "possible"


def test_transit_towards_a_theatre_flags_possible_deployment() -> None:
    pts = _line(50.0, -30.0, 90.0, 10, 70.0, 33000.0, T0)
    th = [{"name": "Europe staging", "lat": 50.0, "lon": 2.0, "radius_km": 1300}]
    act = ma.activity(pts, "airlift", pts[-1][0], idx().nearest, th)
    assert act.label.startswith("Transit heading east towards the Europe staging theatre")
    assert any("deployment" in c for c in act.context)


def test_descending_near_a_field_and_close_to_a_tanker() -> None:
    pts = [(T0 + i * 300, 52.3 + i * 0.02, 0.4, 14000.0 - i * 1800, 250.0) for i in range(5)]
    act = ma.activity(pts, "airlift", pts[-1][0], idx().nearest)
    assert act.label.startswith("Descending, possibly landing at RAF Lakenheath")
    lvl = _line(54.0, 2.0, 90.0, 5, 70.0, 25000.0, T0)
    tanker = [
        {
            "hex": "ae04c5",
            "flight": "QID21",
            "role": "tanker",
            "lat": lvl[-1][1],
            "lon": lvl[-1][2] + 0.03,
            "alt": 25500,
        }
    ]
    act = ma.activity(lvl, "fighter", lvl[-1][0], idx().nearest, others=tanker)
    assert act.label == "Possibly air-to-air refuelling" and "QID21" in act.reasons[0]


def test_type_reference_card() -> None:
    k = miltypes.lookup("k35r")
    assert k is not None and k["name"].startswith("Boeing KC-135")
    assert k["source"].startswith("https://en.wikipedia.org/wiki/")
    assert miltypes.lookup("ZZZZ") is None and miltypes.lookup(None) is None


def test_endpoint(tmp_path: Path) -> None:
    (Path(os.environ["HEIMDALL_AIRPORTS"])).write_text(CSV)
    now = datetime.now(UTC)
    s = MemoryStore()
    s.insert_aircraft(
        [
            AircraftObs(
                ts=now - timedelta(minutes=40 - 5 * i),
                hex="ae04c5",
                provider="adsb.lol",
                lat=52.409 + 0.1 * i,
                lon=0.561 + 0.2 * i,
                alt_baro=None if i == 0 else 2000.0 + 4000 * i,
                on_ground=i == 0,
                gs=0.0 if i == 0 else 300.0,
                mil=True,
                type_code="K35R",
                flight="QID21",
                src_type="adsb_icao",
                is_global=True,
            )
            for i in range(8)
        ]
    )
    c = TestClient(create_app(s, allowed_hosts=["testserver"]))
    r = c.get("/api/military/aircraft/AE04C5")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["role"] == "tanker" and d["country"] == "United States"
    assert d["reference"]["name"].startswith("Boeing KC-135")
    assert d["departure"]["kind"] == "took_off" and d["departure"]["airfield"]["ident"] == "EGUL"
    assert d["activity"]["label"] and d["track"][0][:2] == [0.561, 52.409]
    assert c.get("/api/military/aircraft/abcdef").status_code == 404
    assert c.get("/api/military/aircraft/xyz").status_code == 422
