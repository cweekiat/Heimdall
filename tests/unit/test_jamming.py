from __future__ import annotations

import hashlib
from datetime import timedelta

from heimdall.detect import jamming
from heimdall.geo import h3_cell
from tests.synthetic.generators import (
    T0,
    cell_center,
    jammed_cell,
    mlat_only_traffic,
    single_faulty_transponder,
)

NOW = T0 + timedelta(minutes=30)
CELL = h3_cell(*cell_center(20.0, 96.0), 4)


def _one(evs: list) -> object:
    hits = [e for e in evs if e.h3_cell == CELL]
    assert len(hits) == 1
    return hits[0]


def test_jammed_cell_fires_with_explainable_evidence() -> None:
    evs = jamming.detect(jammed_cell(n_aircraft=12, n_jammed=4), {}, NOW, None)
    ev = _one(evs)
    assert ev.kind == "gnss_jamming"
    assert ev.detector_version == jamming.VERSION
    assert ev.features["bad"] == 4 and ev.features["total"] == 12
    assert abs(ev.features["ratio"] - 3 / 12) < 1e-6
    assert {r.rule for r in ev.rules} >= {"min_aircraft", "degraded_ratio", "robust_z"}
    assert all(r.passed for r in ev.rules)
    assert len(ev.entities) == 4 and len(ev.records) == 8  # 4 aircraft x 2 providers
    assert ev.counterfactual and "<= 2 of 12" in ev.counterfactual
    assert ev.geometry and ev.geometry["type"] == "Polygon"
    assert "no baseline" in " ".join(ev.gaps)
    assert 0 < ev.coverage.confidence <= 1
    raw = f"gnss_jamming||{CELL}||{ev.window_start.isoformat()}"
    assert ev.id == hashlib.sha1(raw.encode()).hexdigest()


def test_ids_deterministic_and_window_aligned() -> None:
    obs = jammed_cell()
    a = _one(jamming.detect(obs, {}, NOW, None))
    b = _one(jamming.detect(obs, {}, NOW + timedelta(minutes=2, seconds=30), None))
    assert a.id == b.id and a.window_end == NOW


def test_single_faulty_transponder_must_not_alert() -> None:
    assert jamming.detect(single_faulty_transponder(n_aircraft=12), {}, NOW, None) == []


def test_mlat_only_cell_cannot_alert() -> None:
    obs = mlat_only_traffic(n_aircraft=20)
    assert jamming.detect(obs, {}, NOW, None) == []
    st = jamming.cell_stats(obs, {}, NOW)[CELL]
    assert st.total == 0 and st.n_observed == 20


def test_sparse_cell_below_min_aircraft() -> None:
    assert jamming.detect(jammed_cell(n_aircraft=4, n_jammed=3), {}, NOW, None) == []


def test_nacp_only_degradation_counts() -> None:
    obs = jammed_cell(n_aircraft=10, n_jammed=4, jam_kw={"nic": None, "nac_p": 3})
    ev = _one(jamming.detect(obs, {}, NOW, None))
    assert ev.features["bad"] == 4


def test_on_ground_excluded() -> None:
    obs = [o.model_copy(update={"on_ground": True}) for o in jammed_cell()]
    assert jamming.detect(obs, {}, NOW, None) == []


def test_baseline_relative_chronic_cell_does_not_fire() -> None:
    chronic = [0.24, 0.25, 0.26, 0.25] * 100

    def base(key: str, metric: str) -> list[float]:
        return chronic

    assert jamming.detect(jammed_cell(), {}, NOW, base) == []


def test_baseline_relative_quiet_cell_fires_with_z() -> None:
    def base(key: str, metric: str) -> list[float]:
        assert metric == jamming.METRIC
        return [0.0] * 400

    ev = _one(jamming.detect(jammed_cell(), {}, NOW, base))
    assert ev.features["robust_z"] >= 3
    assert not any("no baseline" in g for g in ev.gaps)
    rz = next(r for r in ev.rules if r.rule == "robust_z")
    assert rz.baseline == 0.0 and rz.passed


def test_hysteresis_hold_and_clear() -> None:
    # 25 aircraft, 3 degraded -> R' = 2/25 = 0.08: between clear (0.06) and alert (0.10)
    mid = jammed_cell(n_aircraft=25, n_jammed=3)
    assert [e for e in jamming.detect(mid, {}, NOW, None) if e.h3_cell == CELL] == []
    held = _one(jamming.detect(mid, {}, NOW, None, active={CELL}))
    assert any(r.rule == "hysteresis_hold" for r in held.rules)
    assert held.features["held"] == 1.0
    # 25 aircraft, 2 degraded -> R' = 0.04 < 0.06: clears even when active
    low = jammed_cell(n_aircraft=25, n_jammed=2)
    assert [e for e in jamming.detect(low, {}, NOW, None, active={CELL}) if e.h3_cell == CELL] == []


def test_single_provider_lowers_confidence() -> None:
    two = _one(jamming.detect(jammed_cell(providers=("adsb.lol", "adsb.fi")), {}, NOW, None))
    one = _one(jamming.detect(jammed_cell(providers=("adsb.lol",)), {}, NOW, None))
    assert one.coverage.confidence < two.coverage.confidence
    assert any("single provider" in g for g in one.gaps)


def test_window_excludes_old_reports() -> None:
    obs = jammed_cell()
    assert [e for e in jamming.detect(obs, {}, NOW + timedelta(hours=2), None)] == []
