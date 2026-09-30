"""Task 029a: jammer localisation from radio-horizon geometry (jamming-1.2.0)."""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import pytest

from heimdall.detect import jamming
from heimdall.geo import haversine_km
from heimdall.schemas import Evidence
from tests.synthetic.generators import T0, jammed_cell
from tests.synthetic.jammer_geometry import jammer_scene

NOW = T0 + timedelta(minutes=30)
JAM = (20.0, 96.0)
CFG = {"min_aircraft": 5}


def _err(e: Evidence) -> float:
    return haversine_km(e.lat, e.lon, *JAM)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_known_jammer_mixed_altitudes_within_25km(seed: int) -> None:
    evs = jamming.detect(jammer_scene(seed=seed), CFG, NOW, None)
    assert evs
    for e in evs:
        assert e.features["emitter_method"] == 1.0
        assert _err(e) < 25.0, (e.h3_cell, _err(e))
        assert e.features["emitter_agreement"] >= 0.9
        assert e.detector_version == "jamming-1.2.0"


def test_horizon_fit_evidence_is_explainable_and_backwards_compatible() -> None:
    e = jamming.detect(jammer_scene(seed=1), CFG, NOW, None)[0]
    f = e.features
    for k in (
        "emitter_lat",
        "emitter_lon",
        "ellipse_major_km",
        "ellipse_minor_km",
        "ellipse_angle_deg",
    ):
        assert k in f
    assert f["emitter_lat"] == pytest.approx(e.lat, abs=1e-3)
    assert f["emitter_lon"] == pytest.approx(e.lon, abs=1e-3)
    assert 0.0 <= f["emitter_degraded_inside"] <= 1.0 and 0.0 <= f["emitter_healthy_outside"] <= 1.0
    assert f["emitter_n_degraded"] >= 3 and f["emitter_n_candidates"] > 1000
    assert f["emitter_jammer_height_m"] == 10.0
    assert e.geometry and e.geometry["type"] == "Polygon"
    assert e.counterfactual and "radio-horizon fit" in e.counterfactual
    assert not any("centroid" in g for g in e.gaps)
    assert e.bands and e.bands[0].value == "l1_gnss"


def test_fewer_than_3_degraded_aircraft_falls_back_to_centroid() -> None:
    e = jamming.detect(jammed_cell(n_aircraft=8, n_jammed=2), CFG, NOW, None)[0]
    assert e.features["bad"] == 2
    assert e.features["emitter_method"] == 0.0
    assert "emitter_agreement" not in e.features
    assert e.features["ellipse_major_km"] > 0 and e.geometry
    assert any("centroid" in g and "degraded aircraft" in g for g in e.gaps)
    assert "centroid" in (e.counterfactual or "")


def test_missing_altitudes_fall_back_to_centroid() -> None:
    evs = jamming.detect(jammer_scene(seed=2, with_alt=False), CFG, NOW, None)
    assert evs
    assert all(e.features["emitter_method"] == 0.0 for e in evs)
    assert all(any("altitude" in g for g in e.gaps) for e in evs)


def test_disabled_by_config_uses_centroid() -> None:
    evs = jamming.detect(jammer_scene(seed=1), {**CFG, "loc_enabled": False}, NOW, None)
    assert evs and all(e.features["emitter_method"] == 0.0 for e in evs)


def test_colocated_healthy_and_degraded_same_altitude_is_not_trusted() -> None:
    """All aircraft cross one cell at one altitude: geometry cannot separate them -> centroid."""
    e = jamming.detect(jammed_cell(n_aircraft=12, n_jammed=4), {}, NOW, None)[0]
    assert e.features["emitter_method"] == 0.0
    assert any("agreement" in g or "healthy" in g for g in e.gaps)


def test_fast_enough_for_five_minute_tick() -> None:
    obs = jammer_scene(seed=2)
    t0 = time.perf_counter()
    evs = jamming.detect(obs, CFG, NOW, None)
    per_cell = (time.perf_counter() - t0) / max(1, len(evs))
    assert per_cell < 1.0


def test_deterministic() -> None:
    a = jamming.detect(jammer_scene(seed=3), CFG, NOW, None)
    b = jamming.detect(jammer_scene(seed=3), CFG, NOW, None)
    assert [(e.id, e.lat, e.lon) for e in a] == [(e.id, e.lat, e.lon) for e in b]


REPLAY = Path(__file__).resolve().parents[2] / "data/replay/hormuz/2025-06-17.parquet"


@pytest.mark.skipif(not REPLAY.exists(), reason="Hormuz replay day not present")
def test_hormuz_replay_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    from heimdall.backtest.replay_io import read_obs_parquet

    obs = read_obs_parquet(REPLAY)
    assert obs
    t = min(o.ts for o in obs).replace(minute=0, second=0, microsecond=0)
    end = max(o.ts for o in obs)
    n = fits = 0
    while t <= end:
        win = [o for o in obs if t - timedelta(minutes=35) <= o.ts < t + timedelta(minutes=5)]
        for e in jamming.detect(win, {}, t + timedelta(minutes=30), None):
            n += 1
            fits += int(e.features["emitter_method"] == 1.0)
            assert -90.0 <= e.lat <= 90.0 and -180.0 <= e.lon <= 180.0
        t += timedelta(minutes=30)
    with capsys.disabled():
        print(f"hormuz replay: {n} jamming evidences, {fits} horizon-fit estimates")
