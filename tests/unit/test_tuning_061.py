"""061: feedback-driven tuning - precision per detector, threshold proposals, never applied."""

from __future__ import annotations

from datetime import UTC, datetime

from heimdall import tuning
from heimdall.schemas import AlertKind, Coverage, Evidence, RuleResult

NOW = datetime(2026, 10, 4, tzinfo=UTC)
CFG = {"jamming": {"min_aircraft": 5, "ratio_alert": 0.1, "robust_z": 3.0}, "hf_rx": {"z_min": 2.0}}


def ev(det: str, rules: list[tuple[str, float, float, str]], i: int) -> Evidence:
    return Evidence(
        id=f"{det}-{i}",
        kind=AlertKind.GNSS_JAMMING,
        detector=det,
        detector_version="x",
        window_start=NOW,
        window_end=NOW,
        aoi="sg",
        h3_cell="8465a1fffffffff",
        h3_res=4,
        score=0.5,
        rules=[
            RuleResult(rule=r, description=r, value=v, threshold=t, op=op, passed=True)
            for r, v, t, op in rules
        ],
        coverage=Coverage(n_observed=10, n_quality=10, providers=["a"], confidence=0.5),
        created_at=NOW,
    )


def review(i: int, verdict: str | None, n_ac: float, *, reason: str | None = None) -> tuning.Review:
    rules = [("min_aircraft", n_ac, 5, ">="), ("degraded_ratio", 0.2, 0.06, ">=")]
    return tuning.Review(
        f"a{i}", "gnss_jamming", "sg", verdict, reason, {"jamming": [ev("jamming", rules, i)]}
    )


def test_wilson_interval_is_honest_for_small_samples() -> None:
    assert tuning.wilson(0, 0) is None
    lo, hi = tuning.wilson(1, 2)  # type: ignore[misc]
    assert lo < 0.2 and hi > 0.8


def test_proposal_keeps_every_confirmed_alert() -> None:
    rs = [review(i, "real", n) for i, n in enumerate([9, 12, 15])]
    rs += [
        review(10 + i, "false", n, reason="Equipment fault on one aircraft or ship")
        for i, n in enumerate([5, 6, 6, 7, 8, 11])
    ]
    rs += [review(30, None, 5)]  # unreviewed: never counted
    r = tuning.report(rs, CFG, days=30, now=NOW)
    (d,) = r["detectors"]
    assert (d["alerts"], d["reviewed"], d["real"], d["false"], d["unreviewed"]) == (10, 9, 3, 6, 1)
    assert d["precision"] == round(3 / 9, 3) and d["reasons"] == {
        "Equipment fault on one aircraft or ship": 6
    }
    (p,) = d["proposals"]
    assert p["config"] == "detectors.jamming.min_aircraft" and p["current"] == 5
    assert p["proposed"] == 9 and p["false_removed"] == 5 and p["real_kept"] == 3
    assert p["yaml"].startswith("min_aircraft: 9  # 061 feedback")
    assert any("distinct aircraft" in h for h in d["hints"])
    # the ratio rule was judged at the 0.06 clear threshold, not ratio_alert 0.1: never proposed
    assert all(x["rule"] != "degraded_ratio" for x in d["proposals"])


def test_no_proposal_without_enough_reviews_or_gain() -> None:
    few = [review(0, "real", 9), review(1, "false", 5)]
    (d,) = tuning.report(few, CFG, days=30, now=NOW)["detectors"]
    assert d["proposals"] == [] and "not enough reviews" in d["notes"][0]
    # confirmed alerts as low as the false ones: nothing can be cut safely
    rs = [review(i, "real", n) for i, n in enumerate([5, 6, 12])]
    rs += [review(10 + i, "false", 5 + i % 3) for i in range(6)]
    (d,) = tuning.report(rs, CFG, days=30, now=NOW)["detectors"]
    assert d["proposals"] == []


def test_negative_z_rules_map_to_positive_config() -> None:
    def hrx(i: int, verdict: str, z: float) -> tuning.Review:
        e = ev("hf_rx", [("hf_rx_snr_drop", z, -2.0, "<=")], i)
        return tuning.Review(f"h{i}", "spectrum_anomaly", "sg", verdict, None, {"hf_rx": [e]})

    rs = [hrx(i, "real", z) for i, z in enumerate([-4.2, -5.0, -3.6])]
    rs += [hrx(10 + i, "false", z) for i, z in enumerate([-2.1, -2.4, -2.2, -3.0, -2.6])]
    (d,) = tuning.report(rs, CFG, days=30, now=NOW)["detectors"]
    (p,) = d["proposals"]
    assert (
        p["config"] == "detectors.hf_rx.z_min" and p["proposed"] == 3.6 and p["false_removed"] == 5
    )


def test_endpoint_and_markdown() -> None:
    from fastapi.testclient import TestClient

    from heimdall.adapters.memory_store import MemoryStore
    from heimdall.api.app import create_app

    c = TestClient(create_app(MemoryStore(), allowed_hosts=["testserver"]))
    j = c.get("/api/tuning?days=7").json()
    assert j["alerts"] == 0 and "never applied automatically" in j["limits"]
    assert c.get("/api/tuning?days=400").status_code == 422
    assert "| Detector |" in tuning.markdown(j)
