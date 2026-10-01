from __future__ import annotations

from datetime import UTC, datetime, timedelta

from heimdall.detect import DETECTORS, orbit
from heimdall.schemas import AlertKind, Evidence
from tests.synthetic.generators import (
    T0,
    circle_orbit,
    destination,
    heading_wrap_track,
    racetrack,
    straight_flight,
)

NOW = T0 + timedelta(minutes=75)
CFG = {"mil_types": ["K35R", "E3TF"], "tanker_types": ["K35R"]}


def _jam(lat: float, lon: float) -> Evidence:
    return Evidence(
        id="jam1",
        kind=AlertKind.GNSS_JAMMING,
        detector="jamming",
        detector_version="t",
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        lat=lat,
        lon=lon,
        created_at=datetime.now(UTC),
    )


def test_racetrack_60nm_legs_detected_at_75_min() -> None:
    (ev,) = orbit.detect(racetrack(leg_nm=60, minutes=75), CFG, NOW, None)
    assert ev.kind == "mil_orbit" and ev.entities == ["ae0001"]
    assert ev.detector_version == orbit.VERSION
    assert all(r.passed for r in ev.rules if r.rule != "near_jamming")
    assert ev.features["cum_turn_deg"] >= 360 and ev.features["displacement"] <= 0.25
    assert ev.features["is_tanker"] == 1.0
    assert ev.counterfactual and ev.geometry


def test_racetrack_not_yet_at_30_min() -> None:
    now = T0 + timedelta(minutes=30)
    assert orbit.detect(racetrack(leg_nm=60, minutes=30), CFG, now, None) == []


def test_circle_orbit_detected() -> None:
    (ev,) = orbit.detect(circle_orbit(), CFG, NOW, None)
    assert ev.features["circ_var"] > 0.9


def test_straight_flight_not_an_orbit() -> None:
    obs = straight_flight(mil=True, type_code="K35R", minutes=75)
    assert orbit.detect(obs, CFG, NOW, None) == []


def test_heading_wrap_359_1_not_an_orbit() -> None:
    obs = heading_wrap_track()
    assert orbit.detect(obs, CFG, NOW, None) == []
    shape = orbit.track_shape(sorted(obs, key=lambda o: o.ts), orbit._cfg(CFG))
    assert shape["circ_var"] < 0.01 and shape["cum_turn_deg"] < 90


def test_civil_orbit_ignored() -> None:
    obs = circle_orbit(mil=False, type_code="B738")
    assert orbit.detect(obs, CFG, NOW, None) == []


def test_mil_type_list_without_flag() -> None:
    obs = circle_orbit(mil=False, type_code="E3TF")
    assert len(orbit.detect(obs, CFG, NOW, None)) == 1


def test_sparse_track_below_min_points() -> None:
    obs = circle_orbit(dt_s=300)  # 15 points in 75 min
    assert orbit.detect(obs, CFG, NOW, None) == []


def test_mlat_only_orbit_still_detected_with_penalty() -> None:
    adsb = orbit.detect(circle_orbit(), CFG, NOW, None)[0]
    obs = [o.model_copy(update={"src_type": "mlat", "nic": None}) for o in circle_orbit()]
    (ev,) = orbit.detect(obs, CFG, NOW, None)
    assert ev.coverage.confidence < adsb.coverage.confidence
    assert any("MLAT" in g for g in ev.gaps)


def test_novelty_from_baseline() -> None:
    novel = orbit.detect(circle_orbit(), CFG, NOW, lambda k, m: [])[0]
    routine = orbit.detect(circle_orbit(), CFG, NOW, lambda k, m: [1.0] * 2000)[0]
    assert novel.features["novelty"] == 1.0
    assert routine.features["novelty"] < 0.01
    assert novel.score > routine.score


def test_near_jamming_feature() -> None:
    lat, lon = destination(20.0, 96.0, 90, 60)
    (ev,) = orbit.detect(racetrack(), CFG, NOW, None, jam_evidence=[_jam(lat, lon)])
    assert ev.features["near_jamming"] == 1.0 and ev.features["jam_distance_km"] < 100
    far = destination(20.0, 96.0, 90, 300)
    (ev2,) = orbit.detect(racetrack(), CFG, NOW, None, jam_evidence=[_jam(*far)])
    assert ev2.features["near_jamming"] == 0.0


def test_registry() -> None:
    assert set(DETECTORS) == {
        "jamming",
        "spoofing",
        "orbit",
        "ais_spoofing",
        "ais_gap",
        "warning_area",
        "thermal",
        "cygnss",
    }
    assert DETECTORS["orbit"].version == orbit.VERSION
