from __future__ import annotations

from heimdall.detect.ais_gap import detect, find_gaps
from heimdall.schemas import AlertKind, Evidence, VesselObs
from tests.synthetic import vessels as V

TARGET = 111000001


def run(obs: list[VesselObs]) -> list[Evidence]:
    return detect(obs, {}, V.GAP_NOW, None, aoi="scs")


def test_covered_open_gap_fires() -> None:
    (e,) = run(V.dark_gap_covered())
    assert e.kind == AlertKind.AIS_GAP and e.detector_version == "ais-gap-1.1.0"
    assert e.entities == [str(TARGET)]
    f = e.features
    assert abs(f["gap_h"] - 5.0) < 0.1 and f["open"] == 1.0 and f["covered_frac"] >= 0.5
    assert 0 < e.score <= 1 and abs(e.score - min(1, f["gap_h"] / 12) * f["covered_frac"]) < 1e-3
    assert e.counterfactual and e.records and all(r.passed for r in e.rules)
    assert f["anomaly_last_ts"] > f["anomaly_first_ts"]


def test_uncovered_gap_gives_no_evidence() -> None:
    obs = V.gap_uncovered()
    assert run(obs) == []
    reasons = {g["reason"] for g in find_gaps(obs, {}, V.GAP_NOW) if g["mmsi"] == TARGET}
    assert reasons == {"no reception evidence"}


def test_anchored_gap_does_not_fire() -> None:
    obs = V.anchored_gap()
    assert run(obs) == []
    assert {g["reason"] for g in find_gaps(obs, {}, V.GAP_NOW) if g["mmsi"] == TARGET} == {
        "anchored"
    }


def test_short_gap_does_not_fire() -> None:
    assert run(V.short_gap()) == []


def test_closed_gap_reports_distance_and_speed() -> None:
    obs = V.gap_target(silent_h=8.0, resume_after_h=4.0) + V.background_traffic(V.LAT0, V.LON0)
    (e,) = run(obs)
    assert e.features["open"] == 0.0 and abs(e.features["gap_h"] - 4.0) < 0.1
    assert e.features["dist_km"] > 50 and e.features["implied_speed_kn"] > 5
    assert len(e.records) == 2


def test_old_closed_gap_not_reported() -> None:
    obs = V.gap_target(silent_h=20.0, resume_after_h=3.0) + V.background_traffic(V.LAT0, V.LON0)
    assert [x for x in run(obs) if x.entities == [str(TARGET)]] == []


def test_background_traffic_alone_is_quiet() -> None:
    assert run(V.background_traffic(V.LAT0, V.LON0)) == []


def test_open_gap_persists_to_two_ticks() -> None:
    """Pipeline persistence: the same open gap on two consecutive ticks reaches persistence 2."""
    from datetime import timedelta

    from heimdall.config import AOI
    from heimdall.pipeline import _assign_persistence
    from tests.unit.fakes import FakeStore

    store = FakeStore()
    step = timedelta(minutes=5)
    obs = V.dark_gap_covered()
    p = []
    for k in range(2):
        now = V.GAP_NOW + step * k
        evs = detect(obs, {}, now, None, aoi="scs")
        p.append(
            _assign_persistence(store, AOI(name="scs", lat=5, lon=105, radius_nm=250), evs, step)
        )
    assert p[0][0].persistence == 1 and p[1][0].persistence == 2
