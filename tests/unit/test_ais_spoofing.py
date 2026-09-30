from __future__ import annotations

from heimdall.detect.ais_spoofing import DEFAULTS, detect, fit_circle, mmsi_collisions
from heimdall.geo import KM_PER_NM, haversine_km
from heimdall.schemas import AlertKind, Evidence, VesselObs
from tests.synthetic import vessels as V

CFG: dict[str, object] = {}


def run(obs: list[VesselObs]) -> list[Evidence]:
    return detect(obs, CFG, V.NOW, None, aoi="scs")


def signals(evs: list[Evidence]) -> set[str]:
    return {k for e in evs for k in ("A1", "A2", "A3", "A4") if e.features.get(k)}


def test_ring_fires() -> None:
    evs = run(V.ring_spoof())
    assert signals(evs) == {"A3"}
    (e,) = evs
    assert e.kind == AlertKind.AIS_SPOOFING and e.detector_version == "ais-spoofing-1.0.0"
    assert len(e.entities) == 8 and e.features["ring_cv"] <= 0.1
    assert e.counterfactual and e.records and e.aoi == "scs"
    assert all(r.passed for r in e.rules)
    assert "anomaly_first_ts" in e.features and e.features["per_vessel"] == 0.0


def test_ring_needs_enough_vessels_and_spread() -> None:
    assert run(V.ring_spoof(n_vessels=4)) == []
    # all vessels on one side of the centre: bearings concentrated, not a ring
    obs = [
        V.make_vessel(9000 + k, V.T0 + V.timedelta(seconds=30 + k), 5.0 + 0.0001 * k, 105.0 + 0.05)
        for k in range(8)
    ]
    assert "A3" not in signals(run(obs))


def test_circle_three_laps_fires() -> None:
    evs = run(V.circle_track())
    assert signals(evs) == {"A4"}
    e = evs[0]
    assert e.features["laps"] >= 2.0 and abs(e.features["radius_km"] - 1.0) < 0.1
    assert e.features["per_vessel"] == 1.0 and e.entities == ["400000001"]


def test_purse_seine_one_lap_does_not_fire() -> None:
    assert run(V.purse_seine_set()) == []


def test_circle_fit_recovers_radius() -> None:
    import math

    pts = [(2 * math.cos(t / 10), 2 * math.sin(t / 10)) for t in range(63)]
    fit = fit_circle(pts)
    assert fit is not None and abs(fit[2] - 2.0) < 1e-6 and fit[3] < 1e-6
    assert fit_circle([(1.0, 1.0)] * 25) is None  # degenerate


def test_identical_positions_fire() -> None:
    evs = run(V.identical_positions())
    assert signals(evs) == {"A2"} and evs[0].features["n_vessels"] == 3.0


def test_identical_needs_three() -> None:
    assert run(V.identical_positions(n_vessels=2)) == []


def test_rafted_ships_20m_apart_do_not_fire() -> None:
    assert run(V.rafted_ships()) == []


def test_teleport_fires() -> None:
    evs = run(V.teleport())
    assert signals(evs) == {"A1"}
    e = evs[0]
    assert e.features["jump_km"] > 150 and e.features["max_implied_kn"] > DEFAULTS["teleport_kn"]
    assert e.geometry and e.geometry["type"] == "LineString"
    d = haversine_km(e.lat or 0, e.lon or 0, 5.0, 105.0)
    assert d < 20  # localised at the pre-jump anchor


def test_single_glitch_does_not_fire() -> None:
    assert run(V.glitch()) == []


def test_mmsi_collision_guard() -> None:
    obs = V.mmsi_collision()
    assert run(obs) == []
    assert mmsi_collisions(obs, CFG, V.NOW) == {600000001}
    # a real one-way jump is not a collision
    assert mmsi_collisions(V.teleport(), CFG, V.NOW) == set()


def test_anchored_and_transit_do_not_fire() -> None:
    assert run(V.anchored_in_port()) == []
    assert run(V.straight_transit()) == []


def test_invalid_positions_dropped() -> None:
    bad = [V.make_vessel(1, V.T0 + V.timedelta(seconds=60 * i), 0.0, 0.0) for i in range(30)] + [
        V.make_vessel(2, V.T0 + V.timedelta(seconds=60 * i), 95.0, 10.0) for i in range(30)
    ]
    assert run(bad) == []
    # a transit interleaved with (0,0) null-island fixes must not "teleport"
    mixed = V.straight_transit() + [
        V.make_vessel(200000001, V.T0 + V.timedelta(seconds=60 + 120 * i), 0.0, 0.0)
        for i in range(10)
    ]
    assert run(mixed) == []


def test_speed_alone_below_min_distance_does_not_fire() -> None:
    # 3 km hops every 5 s = 2000+ kn implied but under jump_min_km: quantisation, not a jump
    obs = [
        V.make_vessel(5, V.T0 + V.timedelta(seconds=5 * i), 5.0 + 0.027 * (i % 2), 105.0)
        for i in range(40)
    ]
    assert run(obs) == []


def test_units_consistent() -> None:
    assert abs(KM_PER_NM - 1.852) < 1e-9
