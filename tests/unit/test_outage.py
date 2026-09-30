from __future__ import annotations

from typing import Any

from heimdall.config import AOI
from heimdall.detect.outage import VERSION, detect
from heimdall.schemas import AlertKind
from tests.synthetic import outage as syn

MYANMAR = AOI(name="myanmar", lat=20.5, lon=96.5, radius_nm=250)
BALTIC = AOI(name="baltic", lat=56.0, lon=20.5, radius_nm=250)


def run(events: list[Any], cfg: dict[str, Any] | None = None, aoi: AOI = MYANMAR) -> list[Any]:
    return detect(events, cfg or {}, syn.NOW, aoi=aoi)


def test_two_datasource_drop_fires_with_rules_and_features() -> None:
    (e,) = run(syn.ioda_drop())
    assert e.kind == AlertKind.OUTAGE and e.detector_version == VERSION == "outage-1.0.0"
    rules = {r.rule: r for r in e.rules}
    o1, o2 = rules["O1_multi_datasource_drop"], rules["O2_annotation_active"]
    assert o1.passed and o1.value == 2 and o1.threshold == 2 and o1.baseline is not None
    assert not o2.passed
    assert e.features["min_ratio"] == 0.2 and e.features["n_datasources"] == 2
    assert abs(e.features["duration_h"] - 6 * 5 / 60) < 1e-9
    assert e.entities == ["country:MM"] and e.geometry is None and e.h3_cell
    assert e.counterfactual and e.coverage.confidence == 1.0 and e.gaps == []
    assert 0.5 < e.score <= 1.0


def test_single_datasource_blip_does_not_fire() -> None:
    assert run(syn.single_blip()) == []


def test_drops_in_different_bins_do_not_fire() -> None:
    assert run(syn.staggered_drops()) == []


def test_annotation_alone_does_not_fire_by_default() -> None:
    assert run([syn.annotation()]) == []


def test_annotation_alone_fires_when_enabled() -> None:
    (e,) = run([syn.annotation()], {"annotation_alone": True})
    assert not e.rules[0].passed and e.rules[1].passed
    assert e.entities == ["annotation:MM"] and e.coverage.n_observed == 0
    assert e.score < 0.5 and "datasource missing: bgp" in e.gaps


def test_annotation_raises_score_and_supports() -> None:
    plain = run(syn.ioda_drop())[0]
    (e,) = run([*syn.ioda_drop(), syn.annotation()])
    assert e.rules[1].passed and e.score > plain.score
    assert any(r.id.startswith("cloudflare_radar") for r in e.records)


def test_annotation_text_never_copied() -> None:
    (e,) = run([*syn.ioda_drop(), syn.annotation()])
    assert "IGNORE" not in e.model_dump_json()


def test_missing_datasource_lowers_coverage() -> None:
    ev = [x for x in syn.ioda_drop() if x.attrs["datasource"] != "merit-nt"]
    (e,) = run(ev)
    assert "datasource missing: merit-nt" in e.gaps
    assert abs(e.coverage.confidence - 2 / 3) < 1e-9


def test_outside_aoi_and_window_ignored() -> None:
    assert run(syn.ioda_drop(), aoi=BALTIC) == []
    late = detect(syn.ioda_drop(), {}, syn.NOW.replace(hour=23), aoi=MYANMAR)
    assert late == []


def test_thresholds_configurable_and_ratio_from_attrs() -> None:
    ev = syn.ioda_drop()
    assert run(ev, {"drop_ratio_max": 0.1}) == []
    for x in ev:
        x.value = None  # recompute from observed/baseline
    assert len(run(ev)) == 1
