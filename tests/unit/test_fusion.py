from __future__ import annotations

from datetime import timedelta

from heimdall.config import AOI, Settings, playbook, settings
from heimdall.fuse import admiralty
from heimdall.fuse.fusion import actions_for, fuse, severity_for
from heimdall.geo import h3_cell
from heimdall.pipeline import tick
from heimdall.schemas import (
    AlertKind,
    AlertStatus,
    Coverage,
    Domain,
    Evidence,
    RecordRef,
    Severity,
)
from tests.synthetic.generators import (
    T0,
    cell_center,
    destination,
    jammed_cell,
    mlat_only_traffic,
)
from tests.unit.fakes import FakeStore

NOW = T0 + timedelta(minutes=30)
PB = playbook()
GRADES = {"adsb_lol": "B", "adsb_fi": "B", "gpsjam": "B", "faa_nms_notam": "A"}


def ev(
    kind: AlertKind = AlertKind.GNSS_JAMMING,
    cell: str = "8464999ffffffff",
    persistence: int = 2,
    score: float = 0.8,
    conf: float = 0.9,
    lat: float = 20.0,
    lon: float = 96.0,
    ident: str = "e1",
    entities: list[str] | None = None,
    features: dict[str, float] | None = None,
    end_offset_min: int = 0,
    showing: tuple[str, ...] = ("adsb.lol",),
    covering: tuple[str, ...] = ("adsb.lol",),
) -> Evidence:
    end = NOW + timedelta(minutes=end_offset_min)
    return Evidence(
        id=ident,
        kind=kind,
        detector="t",
        detector_version="t-1",
        window_start=end - timedelta(minutes=30),
        window_end=end,
        h3_cell=cell,
        lat=lat,
        lon=lon,
        score=score,
        persistence=persistence,
        entities=entities or [],
        features=features or {"bad": 4, "total": 12, "ratio": 0.25},
        records=[RecordRef(source=p, id=f"{ident}-{p}") for p in showing],
        coverage=Coverage(n_observed=12, n_quality=12, providers=list(covering), confidence=conf),
        created_at=end,
    )


# --------------------------------------------------------------------------- fusion (pure)


def test_persistence_gate() -> None:
    assert fuse([ev(persistence=1)], [], NOW, None, PB, GRADES) == []
    (a,) = fuse([ev(persistence=2)], [], NOW, None, PB, GRADES)
    assert a.kind == "gnss_jamming" and a.evidence_ids == ["e1"]
    assert a.status == AlertStatus.NEW


def test_merge_with_open_alert_updates_last_seen() -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    (b,) = fuse([ev(ident="e2", persistence=3, end_offset_min=5)], [a], NOW, None, PB, GRADES)
    assert b.id == a.id
    assert b.evidence_ids == ["e1", "e2"]
    assert b.last_seen == a.last_seen + timedelta(minutes=5)
    assert b.first_seen == a.first_seen


def test_severity_never_downgrades_while_open() -> None:
    (a,) = fuse([ev(score=0.9, conf=0.9)], [], NOW, None, PB, GRADES)
    assert a.severity == Severity.HIGH
    (b,) = fuse([ev(ident="e2", score=0.2, conf=0.5)], [a], NOW, None, PB, GRADES)
    assert b.severity == Severity.HIGH


def test_severity_from_score_times_confidence() -> None:
    assert severity_for(0.9, 0.9) == Severity.HIGH
    assert severity_for(0.8, 0.5) == Severity.MEDIUM
    assert severity_for(0.5, 0.4) == Severity.LOW
    assert severity_for(0.2, 0.2) == Severity.INFO


def test_control_aoi_capped_at_info() -> None:
    (a,) = fuse([ev(score=1.0, conf=1.0)], [], NOW, None, PB, GRADES, aoi="baltic", control=True)
    assert a.severity == Severity.INFO and a.aoi == "baltic"
    assert "control AOI" in a.summary


def _tanker(i: int, km: float) -> Evidence:
    la, lo = destination(20.0, 96.0, 90.0, km)
    return ev(
        kind=AlertKind.MIL_ORBIT,
        cell=h3_cell(la, lo, 4),
        lat=la,
        lon=lo,
        ident=f"o{i}",
        entities=[f"ae000{i}"],
        features={"is_tanker": 1.0, "laps": 3, "radius_km": 30, "novelty": 1.0},
        score=0.7,
    )


def test_critical_composite_two_tankers_near_jamming() -> None:
    alerts = fuse([ev(), _tanker(1, 40), _tanker(2, 80)], [], NOW, None, PB, GRADES)
    comp = [a for a in alerts if a.kind == AlertKind.COMPOSITE]
    assert len(comp) == 1
    c = comp[0]
    assert c.severity == Severity.CRITICAL
    assert set(c.evidence_ids) == {"e1", "o1", "o2"}
    assert "2 tanker orbits" in c.summary


def test_no_composite_with_one_tanker_or_far_tankers() -> None:
    one = fuse([ev(), _tanker(1, 40)], [], NOW, None, PB, GRADES)
    assert not any(a.kind == AlertKind.COMPOSITE for a in one)
    far = fuse([ev(), _tanker(1, 150), _tanker(2, 200)], [], NOW, None, PB, GRADES)
    assert not any(a.kind == AlertKind.COMPOSITE for a in far)


def test_composite_control_capped() -> None:
    alerts = fuse([ev(), _tanker(1, 40), _tanker(2, 80)], [], NOW, None, PB, GRADES, control=True)
    assert all(a.severity == Severity.INFO for a in alerts)


def test_recommended_actions_from_playbook() -> None:
    jam = actions_for(AlertKind.GNSS_JAMMING, PB)
    assert {"watch_cell", "queue_imagery", "export_brief", "mark_known_area"} <= set(jam)
    spoof = actions_for(AlertKind.GNSS_SPOOFING, PB)
    assert "queue_imagery" not in spoof and "export_brief" in spoof
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    assert a.recommended_actions == jam


def test_template_summary_and_uncorroborated() -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    assert "4/12" in a.summary and "Admiralty B3" in a.summary
    assert a.corroboration_status == "uncorroborated"


def test_colocated_other_kind_is_context_not_corroboration() -> None:
    spoof = ev(kind=AlertKind.GNSS_SPOOFING, ident="s1", entities=["c1"], features={"S1": 1})
    alerts = fuse([ev(), spoof], [], NOW, None, PB, GRADES)
    jam = next(a for a in alerts if a.kind == AlertKind.GNSS_JAMMING)
    assert [c.evidence_id for c in jam.corroborations] == ["s1"]
    assert jam.corroborations[0].relation == "context"
    assert jam.corroboration_status == "uncorroborated"


def test_open_alert_expires_without_fresh_evidence() -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    (x,) = fuse([], [a], NOW + timedelta(minutes=61), None, PB, GRADES)
    assert x.id == a.id and x.status == AlertStatus.EXPIRED
    assert fuse([], [a], NOW + timedelta(minutes=30), None, PB, GRADES) == []


# --------------------------------------------------------------------------- admiralty


def test_admiralty_credibility_ladder() -> None:
    c = admiralty.credibility
    assert c({Domain.AIR, Domain.SEA, Domain.SPACE}) == 1
    assert c({Domain.AIR, Domain.SEA}) == 2
    assert c({Domain.AIR}, strong=True, persistent=True) == 3
    assert c({Domain.AIR}, strong=True, persistent=False) == 4
    assert c(set(), has_evidence=False) == 6
    assert c({Domain.AIR}, analyst_confirmed=True) == 1
    assert c({Domain.AIR, Domain.SEA}, contradicted=True) == 5


def test_admiralty_reliability_best_supported() -> None:
    r = admiralty.reliability
    assert r(["adsb.lol", "unknown.src"], GRADES) == "B"
    assert r(["unknown.src"], GRADES) == "F"
    assert r([], GRADES) == "F"
    assert r(["adsb.lol", "faa-nms-notam"], GRADES) == "A"
    assert admiralty.source_key("airplanes.live") == "airplanes_live"


def test_weak_single_domain_is_c4() -> None:
    (a,) = fuse([ev(score=0.3)], [], NOW, None, PB, GRADES)
    assert str(a.admiralty) == "B4"


# --------------------------------------------------------------------------- pipeline tick

CELL = h3_cell(*cell_center(20.0, 96.0), 4)


def _settings() -> Settings:
    base = settings()
    return Settings(
        aois=[
            AOI(name="myanmar", role="primary", lat=20.0, lon=96.0, radius_nm=250),
            AOI(name="baltic", role="control", lat=56.0, lon=20.5, radius_nm=250),
        ],
        detectors=base.detectors,
    )


def _store(*batches: list) -> FakeStore:
    s = FakeStore()
    for b in batches:
        s.insert_aircraft(b)
    return s


def test_pipeline_persistence_two_ticks_then_alert() -> None:
    store = _store(jammed_cell(n_aircraft=30, n_jammed=10, minutes=40))
    cfg = _settings()
    r1 = tick(store, cfg, T0 + timedelta(minutes=35), playbook=PB, source_grades=GRADES)
    jam1 = [e for e in r1.evidence if e.kind == AlertKind.GNSS_JAMMING and e.h3_cell == CELL]
    assert len(jam1) == 1 and jam1[0].persistence == 1 and jam1[0].aoi == "myanmar"
    assert r1.alerts == []
    r2 = tick(store, cfg, T0 + timedelta(minutes=40), playbook=PB, source_grades=GRADES)
    jam2 = [e for e in r2.evidence if e.kind == AlertKind.GNSS_JAMMING and e.h3_cell == CELL]
    assert jam2[0].persistence == 2
    (alert,) = [a for a in r2.alerts if a.kind == AlertKind.GNSS_JAMMING]
    assert alert.aoi == "myanmar" and alert.severity != Severity.INFO
    assert store.get_alert(alert.id) is not None
    assert len(store.metrics[(CELL, "jam_ratio")]) == 2
    # rerun of the same tick is idempotent (same ids, persistence not inflated)
    r2b = tick(store, cfg, T0 + timedelta(minutes=40), playbook=PB, source_grades=GRADES)
    jam2b = [e for e in r2b.evidence if e.kind == AlertKind.GNSS_JAMMING and e.h3_cell == CELL]
    assert jam2b[0].id == jam2[0].id and jam2b[0].persistence == 2


def test_pipeline_hysteresis_holds_between_thresholds() -> None:
    store = _store(jammed_cell(n_aircraft=30, n_jammed=10, minutes=30))
    cfg = _settings()
    tick(store, cfg, T0 + timedelta(minutes=30), playbook=PB, source_grades=GRADES)
    # next window: only a mid-level ratio (2/25 = 0.08 between clear 0.06 and alert 0.10)
    store.aircraft = jammed_cell(
        start=T0 + timedelta(minutes=5), n_aircraft=25, n_jammed=3, minutes=30, seed=3
    )
    r = tick(store, cfg, T0 + timedelta(minutes=35), playbook=PB, source_grades=GRADES)
    held = [e for e in r.evidence if e.kind == AlertKind.GNSS_JAMMING and e.h3_cell == CELL]
    assert len(held) == 1 and held[0].features["held"] == 1.0 and held[0].persistence == 2
    # a fresh store (no active state) would not fire on the same mid-level data
    fresh = _store(store.aircraft)
    r0 = tick(fresh, cfg, T0 + timedelta(minutes=35), playbook=PB, source_grades=GRADES)
    assert not [e for e in r0.evidence if e.kind == AlertKind.GNSS_JAMMING and e.h3_cell == CELL]


def test_pipeline_mlat_only_cell_cannot_alert() -> None:
    store = _store(mlat_only_traffic(n_aircraft=30))
    cfg = _settings()
    for m in (30, 35, 40):
        r = tick(store, cfg, T0 + timedelta(minutes=m), playbook=PB, source_grades=GRADES)
        assert not [e for e in r.evidence if e.kind == AlertKind.GNSS_JAMMING]
        assert r.alerts == []


def test_pipeline_control_aoi_evidence_but_info_alert() -> None:
    store = _store(jammed_cell(lat=56.0, lon=20.5, n_aircraft=30, n_jammed=10, minutes=40))
    cfg = _settings()
    tick(store, cfg, T0 + timedelta(minutes=35), playbook=PB, source_grades=GRADES)
    r = tick(store, cfg, T0 + timedelta(minutes=40), playbook=PB, source_grades=GRADES)
    assert any(e.aoi == "baltic" for e in r.evidence)
    assert r.alerts and all(a.severity == Severity.INFO and a.aoi == "baltic" for a in r.alerts)
