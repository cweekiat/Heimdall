"""Fusion with cross-source corroboration (ADR-005) and the AIS pipeline path."""

from __future__ import annotations

import json
from datetime import timedelta

from heimdall.config import AOI, Settings, settings
from heimdall.fuse import admiralty
from heimdall.fuse.fusion import fuse
from heimdall.pipeline import AIS_METRIC, tick
from heimdall.schemas import Alert, AlertKind, Severity
from tests.synthetic import vessels as V
from tests.unit.fakes import FakeStore
from tests.unit.test_corroborate import JAM_LAT, JAM_LON, mk
from tests.unit.test_fusion import GRADES, NOW, PB, ev
from tests.unit.test_warning_area import events

CELL = mk(AlertKind.GNSS_JAMMING, "x", JAM_LAT, JAM_LON).h3_cell
GR = {**GRADES, "nga_msi": "A", "aisstream": "B", "gfw": "B"}


def jam(**kw: object) -> object:
    return ev(kind=AlertKind.GNSS_JAMMING, cell=CELL, lat=JAM_LAT, lon=JAM_LON, **kw)  # type: ignore[arg-type]


def notices(*ids: str) -> list:  # type: ignore[type-arg]
    return [e for e in events() if e.id in ids]


def one(alerts: list[Alert], kind: AlertKind) -> Alert:
    (a,) = [x for x in alerts if x.kind == kind]
    return a


def test_jamming_alone_is_uncorroborated_family_grade() -> None:
    a = one(fuse([jam()], [], NOW, None, PB, GR), AlertKind.GNSS_JAMMING)  # type: ignore[list-item]
    assert a.corroboration_status == "uncorroborated" and a.admiralty.credibility == 3


def test_jamming_plus_exercise_navwarn_is_corroborated_cred_2() -> None:
    n = notices("nga-exercise-1")
    a = one(fuse([jam(score=0.3)], [], NOW, None, PB, GR, notices=n), AlertKind.GNSS_JAMMING)  # type: ignore[list-item]
    assert a.corroboration_status == "corroborated" and a.admiralty.credibility == 2
    assert {c.evidence_id for c in a.corroborations if c.relation == "supports"} == {
        "nga-exercise-1"
    }
    assert "Independent source families: adsb, notice" in a.summary


def test_jamming_navwarn_and_ais_spoof_is_cred_1() -> None:
    ais = mk(AlertKind.AIS_SPOOFING, "a1", JAM_LAT + 0.3, JAM_LON, src="aisstream")
    ais = ais.model_copy(update={"features": {"A3": 1.0}, "entities": ["1", "2"]})
    alerts = fuse([jam(), ais], [], NOW, None, PB, GR, notices=notices("nga-exercise-1"))  # type: ignore[list-item]
    a = one(alerts, AlertKind.GNSS_JAMMING)
    assert a.admiralty.credibility == 1 and a.corroboration_status == "corroborated"
    s = one(alerts, AlertKind.AIS_SPOOFING)  # the AIS alert is supported by jamming (adsb family)
    assert s.corroboration_status == "corroborated" and s.admiralty.credibility == 2
    assert "AIS spoofing" in s.summary


def test_orbit_near_jamming_alone_is_cred_3_same_family() -> None:
    j = jam(score=0.3)
    o = ev(
        kind=AlertKind.MIL_ORBIT,
        ident="o1",
        cell="8464998ffffffff",
        lat=JAM_LAT + 0.2,
        lon=JAM_LON,
        score=0.3,
        entities=["ae1"],
        features={"is_tanker": 0.0},
    )
    a = one(fuse([j, o], [], NOW, None, PB, GR), AlertKind.MIL_ORBIT)  # type: ignore[list-item]
    assert a.admiralty.credibility == 3 and a.corroboration_status == "uncorroborated"
    lone = one(fuse([o], [], NOW, None, PB, GR), AlertKind.MIL_ORBIT)
    assert lone.admiralty.credibility == 4


def test_notice_text_never_in_alert() -> None:
    a = one(
        fuse([jam()], [], NOW, None, PB, GR, notices=notices("nga-exercise-1", "faa-gnss-1")),  # type: ignore[list-item]
        AlertKind.GNSS_JAMMING,
    )
    blob = json.dumps(a.model_dump(mode="json"))
    assert "IGNORE ALL PREVIOUS" not in blob and "script" not in blob
    assert "HYDROPAC 1234/26" in blob


def test_warning_evidence_never_opens_alert() -> None:
    w = mk(AlertKind.WARNING_AREA, "w1", JAM_LAT, JAM_LON, src="nga_msi")
    assert fuse([w], [], NOW, None, PB, GR) == []


def test_control_aoi_cap_unchanged_for_ais() -> None:
    ais = mk(AlertKind.AIS_SPOOFING, "a1", JAM_LAT, JAM_LON, src="aisstream")
    (a,) = fuse([ais], [], NOW, None, PB, GR, control=True)
    assert a.severity == Severity.INFO


def test_admiralty_grade_backward_compatible_and_family_arg() -> None:
    e = ev()
    assert admiralty.grade([e], [], GR, required_persistence=2, strong_score=0.6).credibility == 3
    g = admiralty.grade(
        [e], [], GR, required_persistence=2, strong_score=0.6, families={"adsb", "notice"}
    )
    assert g.credibility == 2
    g3 = admiralty.grade(
        [ev(score=0.2)],
        [],
        GR,
        required_persistence=2,
        strong_score=0.6,
        families={"adsb"},
        same_family_support=True,
    )
    assert g3.credibility == 3


# --------------------------------------------------------------------------- pipeline


def _settings() -> Settings:
    base = settings()
    return Settings(
        aois=[AOI(name="scs", role="primary", lat=V.LAT0, lon=V.LON0, radius_nm=250)],
        detectors=base.detectors,
    )


def test_pipeline_ais_gap_alert_after_two_ticks_with_gfw_corroboration() -> None:
    store = FakeStore()
    store.insert_vessels(V.dark_gap_covered())
    store.insert_events(events())
    cfg = _settings()
    r1 = tick(store, cfg, V.GAP_NOW, playbook=PB, source_grades=GR)
    g1 = [e for e in r1.evidence if e.kind == AlertKind.AIS_GAP]
    assert len(g1) == 1 and g1[0].persistence == 1 and r1.alerts == []
    r2 = tick(store, cfg, V.GAP_NOW + timedelta(minutes=5), playbook=PB, source_grades=GR)
    (a,) = [x for x in r2.alerts if x.kind == AlertKind.AIS_GAP]
    assert a.aoi == "scs" and a.corroboration_status == "corroborated"
    assert "gfw-gap-1" in {c.evidence_id for c in a.corroborations if c.relation == "supports"}
    assert store.metric_history(g1[0].h3_cell or "", AIS_METRIC, V.GAP_NOW - timedelta(days=1))


def test_pipeline_warning_evidence_stored_but_no_alert() -> None:
    store = FakeStore()
    store.insert_events(events())
    cfg = Settings(
        aois=[AOI(name="south_china_sea", lat=12.5, lon=113.5, radius_nm=250)],
        detectors=settings().detectors,
    )
    now = V.T0 + timedelta(minutes=30)
    for k in range(3):
        r = tick(store, cfg, now + timedelta(minutes=5 * k), playbook=PB, source_grades=GR)
    assert {e.kind for e in r.evidence} == {AlertKind.WARNING_AREA} and r.alerts == []
    assert len(r.evidence) == 2


def test_pipeline_ais_spoof_ring_reaches_alert() -> None:
    store = FakeStore()
    store.insert_vessels(V.ring_spoof(n_bins=12))  # 60 min of a static ring
    cfg = _settings()
    alerts: list[Alert] = []
    for k in range(3):
        r = tick(store, cfg, V.NOW + timedelta(minutes=5 * k), playbook=PB, source_grades=GR)
        alerts += r.alerts
    assert any(a.kind == AlertKind.AIS_SPOOFING for a in alerts)
