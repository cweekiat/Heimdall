"""056: air force watch - flights, where each aircraft is, movements, notes."""

from __future__ import annotations

from datetime import UTC, datetime

from heimdall import airforce
from heimdall.ingest.ourairports import AirfieldIndex, parse_csv

HEAD = (
    '"id","ident","type","name","latitude_deg","longitude_deg","elevation_ft","continent",'
    '"iso_country","iso_region","municipality","scheduled_service","icao_code","iata_code",'
    '"gps_code","local_code","home_link","wikipedia_link","keywords"'
)
ROWS = [
    '1,"WSAP","medium_airport","Paya Lebar Air Base",1.360,103.910,65,"AS","SG","SG-01","","no",'
    '"WSAP",,,,,,',
    '2,"YPDN","large_airport","Darwin International Airport",-12.415,130.877,103,"OC","AU","AU-NT",'
    '"","yes","YPDN",,,,,,',
    '3,"YPEA","medium_airport","RAAF Base Pearce",-31.668,116.015,149,"OC","AU","AU-WA","","no",'
    '"YPEA",,,,,,',
]
IDX = AirfieldIndex(parse_csv("\n".join([HEAD, *ROWS])))
CFG = {
    "home_country": "SG",
    "home_bases": {"WSAP": "Paya Lebar Air Base"},
    "detachments": {"YPEA": {"name": "RAAF Pearce, Australia", "unit": "PC-21 training"}},
    "watch_types": ["A332"],
}
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
T = NOW.timestamp()


def _ferry() -> list[tuple[float, float, float, float, float]]:
    """Paya Lebar -> Darwin, landed 6 h ago."""
    start = T - 12 * 3600
    pts = [(start, 1.360, 103.910, -1.0, 0.0)]
    for i in range(1, 20):
        f = i / 20
        pts.append(
            (start + i * 900, 1.36 + f * (-12.415 - 1.36), 103.91 + f * 26.97, 33000.0, 460.0)
        )
    pts.append((start + 20 * 900, -12.41, 130.88, 1500.0, 160.0))
    pts.append((start + 20 * 900 + 120, -12.415, 130.877, -1.0, 0.0))
    return pts


def _pearce_sortie(t0: float) -> list[tuple[float, float, float, float, float]]:
    return [
        (t0, -31.668, 116.015, -1.0, 0.0),
        (t0 + 600, -31.5, 116.2, 9000.0, 250.0),
        (t0 + 1800, -31.6, 116.1, 1200.0, 140.0),
        (t0 + 1900, -31.668, 116.015, -1.0, 0.0),
    ]


def test_fleet_picture_home_abroad_detachment_airborne() -> None:
    fleet = {
        "76c6a1": {"type": "A332", "type_name": "Airbus A330 MRTT", "registration": "762"},
        "76e721": {"type": "PC21", "registration": "PC1"},
        "76e722": {"type": "PC21", "registration": "PC2"},
    }
    tracks = {
        "76c6a1": _ferry(),
        "76e721": _pearce_sortie(T - 5 * 3600),
        "76e722": [(T - 300, -31.4, 116.3, 8000.0, 240.0), (T - 60, -31.45, 116.25, 8200.0, 240.0)],
    }
    d = airforce.analyse(fleet, tracks, IDX.nearest, CFG, NOW)
    st = {a["hex"]: a for a in d["aircraft"]}
    assert st["76c6a1"]["status"] == "abroad" and st["76c6a1"]["place"]["ident"] == "YPDN"
    assert (
        st["76e721"]["status"] == "detachment" and st["76e721"]["place"]["unit"] == "PC-21 training"
    )
    assert st["76e722"]["status"] == "airborne"
    (mv,) = d["movements"]
    assert mv["dep"]["ident"] == "WSAP" and mv["arr"]["ident"] == "YPDN"
    texts = [n["text"] for n in d["notes"]]
    assert any("routine overseas training detachment" in t for t in texts)
    assert any("possible deployment, exercise or transport mission" in t for t in texts)
    assert any("Paya Lebar Air Base (SG) → Darwin International Airport (AU)" in t for t in texts)


def test_local_sorties_are_not_movements_and_busy_day_is_flagged() -> None:
    fleet = {"76e721": {"type": "PC21"}}
    pts = []
    for day in range(4, 0, -1):  # one sortie a day for 3 days, then 5 today
        pts += _pearce_sortie(T - day * 86400)
    for k in range(5):
        pts += _pearce_sortie(T - 10 * 3600 + k * 2000)
    d = airforce.analyse(fleet, {"76e721": pts}, IDX.nearest, CFG, NOW)
    assert d["movements"] == []
    assert any("above usual" in n["text"] for n in d["notes"])


def test_endpoints() -> None:
    from fastapi.testclient import TestClient

    from heimdall.adapters.memory_store import MemoryStore
    from heimdall.api.app import create_app

    c = TestClient(create_app(MemoryStore(), allowed_hosts=["testserver"]))
    assert any(f["key"] == "rsaf" for f in c.get("/api/airforces").json())
    d = c.get("/api/airforces/rsaf?days=1").json()
    assert d["short"] == "RSAF" and "limits" in d and d["seen"] == 0
    assert c.get("/api/airforces/nope").status_code == 404


def test_058_asia_pacific_forces_configured_and_exclude(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from heimdall.api import airforce as api_af

    fs = api_af.forces()
    for k in ("rsaf", "rmaf", "tni_au", "paf", "rtaf", "rocaf", "jasdf", "rokaf", "raaf", "rnzaf"):
        f = fs[k]
        assert f["operators"] and len(f["home_country"]) == 2 and f["home_bases"] and f["limits"]
        assert isinstance(f["detachments"], dict) and "A400" in f["watch_types"]
    idx = {
        "7c0001": {"operator": "Royal Australian Air Force", "icao_type": "C17"},
        "7c0002": {"operator": "Royal Australian Air Force Museum", "icao_type": "F86"},
        "7c0003": {"operator": "Australian Air Force", "icao_type": "P8"},
    }
    monkeypatch.setattr(api_af.plane_alert, "index", lambda: idx)
    assert sorted(api_af.fleet_of(fs["raaf"])) == ["7c0001", "7c0003"]
