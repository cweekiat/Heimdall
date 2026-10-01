"""Task 039: military identity, /v2/mil connector, track store, theatre detectors, tick wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from heimdall.config import AOI, Settings, settings
from heimdall.detect import military as mil
from heimdall.ingest.adsb_mil import AdsbMilConnector
from heimdall.ingest.plane_alert import index, parse_csv
from heimdall.milroles import classify, country_of
from heimdall.miltrack import MilTracks
from heimdall.pipeline import tick
from heimdall.schemas import AircraftObs, AlertKind, Severity
from tests.unit.fakes import FakeStore
from tests.unit.test_fusion import GRADES, PB

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
CSV = (
    "$ICAO,$Registration,$Operator,$Type,$ICAO Type,#CMPG,$Tag 1,$#Tag 2,$#Tag 3,Category,$#Link\n"
    "AE0001,58-0069,United States Air Force,Boeing KC-135R,K35R,Mil,Air2Air,Stratotanker,,USAF,x\n"
    "AE0002,12-0001,United States Air Force,Airbus A330 MRTT,A332,Mil,Air2Air,,,USAF,x\n"
    "06A021,A7-HHF,Qatar Amiri Flight,Airbus A340-500,A345,Gov,Dictator Alert,,,Dictator Alert,x\n"
    "AE0003,N1,Police,Cessna 182,C182,Pol,Surveillance,,,Police Forces,x\n"
    "XYZ,bad,row,,,Mil,,,,,\n"
)
EUROPE = mil.Theatre("Europe staging", 45.5, 2.0, 1300.0)


def test_classify_prefers_curated_entry_then_type() -> None:
    db = parse_csv(CSV)
    assert set(db) == {"ae0001", "ae0002", "06a021", "ae0003"}  # malformed hex dropped
    assert classify("ae0002", "A332", db["ae0002"]).role == "tanker"  # MRTT only via its tag
    assert classify("ae9999", "A332", None).role == "other"  # an A330 alone is an airliner
    q = classify("06a021", "A345", db["06a021"])
    assert q.role == "gov_vip" and q.high_interest and q.country == "Qatar" and q.known
    assert classify("ae0003", "C182", db["ae0003"]).role == "other"  # police, not ISR
    assert classify("ae0004", "E6", None).role == "c2" and classify("ae0004", "E6").high_interest
    assert classify("ae0005", "B52").role == "bomber"
    assert country_of("43c000") == "United Kingdom" and country_of("~1234") is None


def test_index_reads_file_once(tmp_path: Path) -> None:
    p = tmp_path / "pa.csv"
    p.write_text(CSV)
    assert len(index(p)) == 4 and index(p) is index(p)
    assert index(tmp_path / "missing.csv") == {}


@respx.mock
async def test_adsb_mil_falls_back_and_marks_rows() -> None:
    respx.get("https://a.invalid/v2/mil").mock(return_value=httpx.Response(503))
    body = {
        "ac": [
            {
                "hex": "ae0001",
                "lat": 50.0,
                "lon": 5.0,
                "alt_baro": 30000,
                "t": "K35R",
                "dbFlags": 1,
                "type": "adsb_icao",
                "seen_pos": 1,
            }
        ]
    }
    respx.get("https://b.invalid/v2/mil").mock(return_value=httpx.Response(200, json=body))
    c = AdsbMilConnector(
        "adsb_mil", [("a", "https://a.invalid/v2/mil"), ("b", "https://b.invalid/v2/mil")]
    )
    (o,) = await c.poll_once()
    assert o.is_global and o.mil and o.provider == "b" and c.provider_used == "b"
    assert c.health().status == "ok"
    await c.aclose()


def _obs(hex_: str, t: datetime, lat: float, lon: float, tc: str = "K35R") -> AircraftObs:
    return AircraftObs(
        ts=t,
        hex=hex_,
        provider="adsb.lol",
        lat=lat,
        lon=lon,
        src_type="adsb_icao",
        type_code=tc,
        mil=True,
        alt_baro=30000,
        gs=450.0,
        is_global=True,
    )


def _crossing(
    hex_: str,
    tc: str = "K35R",
    start: datetime = NOW - timedelta(hours=20),
    step: timedelta = timedelta(hours=1),
) -> list[AircraftObs]:
    """A transatlantic hop: Azores-ish (well outside Europe staging) -> Ramstein-ish (inside)."""
    pts = [(38.0, -40.0), (42.0, -25.0), (46.0, -10.0), (48.5, 2.0), (49.4, 7.6)]
    return [_obs(hex_, start + step * k, la, lo, tc) for k, (la, lo) in enumerate(pts)]


def test_tracks_decimate_prune_and_roundtrip() -> None:
    t = MilTracks(step_s=300)
    t.add([_obs("ae0001", NOW + timedelta(seconds=s), 50, 5) for s in (0, 60, 120, 400)])
    assert len(t.tracks["ae0001"].points) == 2  # one per 5 min
    back = MilTracks.from_json(t.to_json())
    assert back.tracks["ae0001"].points == t.tracks["ae0001"].points
    t.prune(NOW + timedelta(hours=80))
    assert t.tracks == {}


def _idents(t: MilTracks) -> dict:
    return {h: classify(h, tr.type_code) for h, tr in t.tracks.items()}


def test_deployment_wave_fires_on_tankers_flowing_in() -> None:
    t = MilTracks()
    for i in range(7):
        t.add(_crossing(f"ae10{i:02d}"))
    t.add([_obs("ae2000", NOW - timedelta(hours=1), 50, 5)])  # already there: not an arrival
    snap = mil.Snapshot.of(t)
    evs, counts = mil.detect_movements(snap, EUROPE, _idents(t), NOW)
    (d,) = [e for e in evs if e.kind == AlertKind.MIL_DEPLOYMENT]
    assert d.features["n"] == 7 and d.features["role_tanker"] == 1.0
    assert d.rules[0].passed and "7 tankers flew into Europe staging" in d.rules[0].description
    assert "ae2000" not in d.entities and len(d.entities) == 7
    assert any("fly dark" in g for g in d.gaps)
    assert counts["tanker"] == 1.0  # airborne inside in the last 6 h: only the resident one


def test_few_arrivals_do_not_fire() -> None:
    t = MilTracks()
    for i in range(3):
        t.add(_crossing(f"ae10{i:02d}"))
    evs, _ = mil.detect_movements(mil.Snapshot.of(t), EUROPE, _idents(t), NOW)
    assert evs == []


def test_surge_needs_baseline_and_ratio() -> None:
    t = MilTracks()
    t.add(
        [_obs(f"ae30{i:02d}", NOW - timedelta(hours=1), 50 + i * 0.1, 5, "C17") for i in range(9)]
    )
    snap, ids = mil.Snapshot.of(t), _idents(t)
    assert mil.detect_movements(snap, EUROPE, ids, NOW, baseline=lambda r: [])[0] == []  # learning
    quiet = [1.0, 2.0, 1.0, 2.0] * 20
    evs, _ = mil.detect_movements(snap, EUROPE, ids, NOW, baseline=lambda r: quiet)
    (s,) = [e for e in evs if e.kind == AlertKind.MIL_SURGE]
    assert s.features["n"] == 9 and s.rules[0].baseline == 1.5
    busy = [8.0, 9.0, 10.0] * 30
    assert mil.detect_movements(snap, EUROPE, ids, NOW, baseline=lambda r: busy)[0] == []


def test_high_interest_and_buildup_checklist() -> None:
    t = MilTracks()
    for i in range(7):
        t.add(_crossing(f"ae10{i:02d}"))
    t.add([_obs("ae4000", NOW - timedelta(minutes=10), 51, 0, "E6")])
    t.add([_obs(f"ae50{i}", NOW - timedelta(hours=3), 47, 3, "R135") for i in range(2)])
    snap, ids = mil.Snapshot.of(t), _idents(t)
    evs, _ = mil.detect_movements(snap, EUROPE, ids, NOW)
    (hi,) = [e for e in evs if e.kind == AlertKind.MIL_HIGH_INTEREST]
    assert hi.entities == ["ae4000"] and hi.features["strategic"] == 1.0
    (b,) = mil.detect_buildup(evs, snap, EUROPE, ids, NOW, n_notices=1)
    passed = {r.rule for r in b.rules if r.passed}
    assert passed == {"B_tanker", "B_bomber_c2", "B_isr", "B_notice"}
    assert "never predicts a target" in " ".join(b.gaps)
    # indicators without any movement never make a build-up
    assert mil.detect_buildup([], snap, EUROPE, ids, NOW, n_notices=3, n_jamming=2) == []


def test_tick_opens_deployment_alert_for_theatre() -> None:
    store = FakeStore()
    for i in range(7):
        store.insert_aircraft(
            _crossing(f"ae10{i:02d}", start=NOW - timedelta(minutes=70), step=timedelta(minutes=15))
        )
    det = {**settings().detectors}
    det["military"] = {
        **det["military"],
        "outside_margin_km": 50,
        "theatres": [{"name": "Europe staging", "lat": 45.5, "lon": 2.0, "radius_km": 1300}],
    }
    cfg = Settings(aois=[AOI(name="gulf", lat=26, lon=51.5, radius_nm=200)], detectors=det)
    # the crossing compressed into 70 min, so a 75-min cold-start read sees all of it
    for k in range(3):
        r = tick(
            store,
            cfg,
            NOW + timedelta(minutes=5 * k),
            playbook=PB,
            source_grades=GRADES,
            sources_cfg={},
        )
    alerts = [a for a in r.alerts if a.kind == AlertKind.MIL_DEPLOYMENT]
    stored = [a for a in store.list_alerts(limit=100) if a.kind == AlertKind.MIL_DEPLOYMENT]
    assert alerts == [] and len(stored) == 1  # theatre alerts are written by the military step
    a = stored[0]
    assert a.aoi == "Europe staging" and a.title.startswith("Military deployment wave")
    assert a.severity in (Severity.LOW, Severity.MEDIUM, Severity.HIGH)
    assert "7 tankers flew into Europe staging" in a.summary


@pytest.mark.parametrize("theatre_count", [16])
def test_config_theatres_load(theatre_count: int) -> None:
    th = settings().detectors["military"]["theatres"]
    assert len(th) == theatre_count and all(t["radius_km"] <= 1300 for t in th)


def test_api_summary_counts_roles_and_theatres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from heimdall.api.military import military_summary

    p = tmp_path / "pa.csv"
    p.write_text(CSV)
    monkeypatch.setenv("HEIMDALL_PLANE_ALERT", str(p))
    store = FakeStore()
    store.insert_aircraft(
        [
            _obs("ae0001", NOW - timedelta(minutes=2), 49.4, 7.6),
            _obs("ae0001", NOW - timedelta(minutes=1), 49.5, 7.7),  # newest wins
            _obs("06a021", NOW - timedelta(minutes=3), 25.3, 51.6, "A345"),
            _obs("ae7777", NOW - timedelta(minutes=30), 0, 0, "C17"),  # too old for the window
        ]
    )
    s = military_summary(store, NOW, 10)
    assert s["contacts"] == 2 and s["identified"] == 2 and s["roles"] == {"tanker": 1, "gov_vip": 1}
    (q,) = s["high_interest"]
    assert q["operator"] == "Qatar Amiri Flight" and q["country"] == "Qatar"
    tanker = next(a for a in s["aircraft"] if a["hex"] == "ae0001")
    assert tanker["lat"] == 49.5 and tanker["type_name"] == "Boeing KC-135R"
    th = {t["name"]: t for t in s["theatres"]}
    assert th["Europe staging"]["roles"] == {"tanker": 1} and th["Gulf"]["n"] == 1
    assert "ODbL" in s["attribution"]
