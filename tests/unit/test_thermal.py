from __future__ import annotations

from datetime import timedelta
from typing import Any

from heimdall.config import AOI
from heimdall.detect.thermal import VERSION, detect
from heimdall.schemas import AlertKind, Evidence, GeoEvent
from tests.synthetic import thermal as syn

SCS = AOI(name="south_china_sea", lat=12.5, lon=113.5, radius_nm=250)
CFG: dict[str, Any] = {}
Scenario = tuple[list[GeoEvent], list[GeoEvent]]


def run(sc: Scenario, cfg: dict[str, Any] = CFG, aoi: AOI = SCS) -> list[Evidence]:
    ev, base = sc
    return detect(ev, base, cfg, syn.NOW, aoi=aoi)


def test_new_cluster_fires_with_rules_and_records() -> None:
    out = run(syn.new_cluster())
    assert len(out) == 1
    e = out[0]
    assert e.kind == AlertKind.THERMAL and e.detector_version == VERSION == "thermal-1.0.0"
    rules = {r.rule: r for r in e.rules}
    assert set(rules) == {"T1_min_detections", "T2_frp_sum", "T3_baseline_ratio"}
    assert all(r.passed and r.value is not None and r.threshold is not None for r in e.rules)
    assert rules["T3_baseline_ratio"].baseline is not None
    assert rules["T1_min_detections"].value == 5 and rules["T2_frp_sum"].value == 150
    assert len(e.records) == 5 and e.counterfactual and "ratio" in e.counterfactual
    assert e.coverage.n_observed == 5 and e.coverage.confidence == 1.0
    assert 0.0 < e.score <= 1.0 and e.aoi == "south_china_sea" and e.h3_res == 6
    assert all("short baseline" not in g for g in e.gaps)


def test_gas_flare_masked() -> None:
    assert run(syn.gas_flare()) == []


def test_flare_masked_but_new_cluster_nearby_still_fires() -> None:
    ev, base = syn.new_cluster()
    fev, fbase = syn.gas_flare(dlat=-0.02)
    out = detect(ev + fev, base + fbase, CFG, syn.NOW, aoi=SCS)
    assert len(out) == 1
    assert any("static heat source masked: 4" in g for g in out[0].gaps)


def test_seasonal_burning_matching_baseline_does_not_fire() -> None:
    assert run(syn.seasonal_burning()) == []


def test_lone_low_confidence_pixel_ignored() -> None:
    assert run(syn.lone_low_confidence()) == []
    all_conf = {"min_confidence": ["low", "nominal", "high"]}
    assert run(syn.lone_low_confidence(), all_conf) == []  # one pixel: T1 fails, FRP 80 < ... T3


def test_one_pass_cluster_fires_with_low_coverage() -> None:
    (e,) = run(syn.one_pass_cluster())
    assert e.coverage.confidence <= 0.25 and e.coverage.n_observed == 1
    assert e.score <= 0.25 and any("expected satellite passes" in g for g in e.gaps)


def test_short_baseline_caps_score_and_adds_gap() -> None:
    ev, base = syn.new_cluster()
    short = [b for b in base if b.ts > syn.NOW - timedelta(days=20)]
    (e,) = detect(ev, short, CFG, syn.NOW, aoi=SCS)
    assert any(g.startswith("short baseline") for g in e.gaps) and e.score <= 0.6
    (e2,) = detect(ev, [], CFG, syn.NOW, aoi=SCS)
    assert any(g.startswith("short baseline") for g in e2.gaps)


def test_outside_aoi_and_outside_window_ignored() -> None:
    other = AOI(name="baltic", lat=56.0, lon=20.5, radius_nm=250)
    assert run(syn.new_cluster(), aoi=other) == []
    ev, base = syn.new_cluster()
    old = [e.model_copy(update={"ts": e.ts - timedelta(hours=30)}) for e in ev]
    assert detect(old, base, CFG, syn.NOW, aoi=SCS) == []


def test_deterministic_and_untrusted_text_not_leaked() -> None:
    a, b = run(syn.new_cluster()), run(syn.new_cluster())
    assert [x.id for x in a] == [x.id for x in b]
    assert "IGNORE" not in a[0].model_dump_json()
