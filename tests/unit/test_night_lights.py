from __future__ import annotations

from typing import Any

from heimdall.config import AOI
from heimdall.detect.night_lights import VERSION, detect
from heimdall.schemas import AlertKind
from tests.synthetic import outage as syn

MYANMAR = AOI(name="myanmar", lat=20.5, lon=96.5, radius_nm=250)
BALTIC = AOI(name="baltic", lat=56.0, lon=20.5, radius_nm=250)


def run(events: list[Any], cfg: dict[str, Any] | None = None, aoi: AOI = MYANMAR) -> list[Any]:
    return detect(events, cfg or {}, syn.NOW, aoi=aoi)


def test_city_dark_three_nights_fires() -> None:
    (e,) = run(syn.city_dark(3))
    assert e.kind == AlertKind.OUTAGE and e.detector_version == VERSION == "night-lights-1.0.0"
    r = {x.rule: x for x in e.rules}
    assert set(r) == {
        "N1_lit_baseline",
        "N2_drop_ratio",
        "N3_valid_fraction",
        "N4_consecutive_nights",
    }
    assert all(x.passed for x in e.rules)
    assert r["N4_consecutive_nights"].value == 3 and r["N2_drop_ratio"].value < 0.5
    assert r["N1_lit_baseline"].baseline is not None and 29 < r["N1_lit_baseline"].value < 31
    assert 0.1 < e.features["night_lights_ratio"] < 0.2
    assert "imagery lag: 2 days" in e.gaps and e.h3_cell == syn.CITY_CELL
    assert e.coverage.confidence > 0.8 and e.counterfactual and e.records


def test_two_nights_is_enough_one_is_not() -> None:
    assert len(run(syn.city_dark(2))) == 1
    assert run(syn.city_dark(1)) == []


def test_one_cloudy_night_does_not_fire() -> None:
    assert run(syn.one_cloudy_night()) == []


def test_cloudy_night_inside_run_is_skipped_not_breaking() -> None:
    (e,) = run(syn.cloudy_in_run())
    assert e.features["n_dropped_nights"] == 2
    assert any("excluded" in g for g in e.gaps)


def test_unlit_cell_does_not_fire() -> None:
    assert run(syn.unlit_rural()) == []


def test_slow_dimming_does_not_fire() -> None:
    assert run(syn.slow_dimming()) == []


def test_outage_does_not_poison_baseline_and_recovery_resets() -> None:
    ev = syn.city_dark(3)
    recovered = [*ev, syn.night(1, 30.0), syn.night(0, 30.0)]
    assert run(recovered) == []


def test_stale_imagery_and_outside_aoi() -> None:
    assert run(syn.city_dark(3, lag=15)) == []
    assert run(syn.city_dark(3), aoi=BALTIC) == []


def test_short_history_has_no_evidence_or_gap() -> None:
    few = [syn.night(4, 30.0), syn.night(3, 30.0), syn.night(2, 5.0), syn.night(1, 5.0)]
    assert run(few) == []  # < min_history_nights clean nights: baseline not trusted
    (e,) = run(syn.city_dark(3)[-12:])
    assert any(g.startswith("short baseline") for g in e.gaps)


def test_untrusted_title_not_leaked_and_deterministic() -> None:
    a, b = run(syn.city_dark(3)), run(syn.city_dark(3))
    assert a[0].id == b[0].id and "IGNORE" not in a[0].model_dump_json()
