"""Regression tests for the task-003 review (ADR-003 statuses, persistence, ids, metrics)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from heimdall.fuse.fusion import apply_evidence_update, fuse
from heimdall.pipeline import tick
from heimdall.schemas import AlertKind, AlertStatus
from tests.synthetic.generators import T0, jammed_cell
from tests.unit.test_fusion import GRADES, NOW, PB, _settings, _store, ev

ANALYST = [
    AlertStatus.ACKNOWLEDGED,
    AlertStatus.UNDER_REVIEW,
    AlertStatus.SHELVED,
    AlertStatus.CONFIRMED,
]


# --------------------------------------------------------------------------- 1. ADR-003


@pytest.mark.parametrize("status", ANALYST)
def test_fusion_matches_analyst_status_without_duplicate(status: AlertStatus) -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    held = a.model_copy(update={"status": status})
    (b,) = fuse([ev(ident="e2", end_offset_min=5)], [held], NOW, None, PB, GRADES)
    assert b.id == a.id and b.status == status
    assert b.evidence_ids == ["e1", "e2"]


def test_dismissed_alert_is_closed_new_alert_opens() -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    gone = a.model_copy(update={"status": AlertStatus.DISMISSED})
    (b,) = fuse([ev(ident="e2", end_offset_min=5)], [gone], NOW, None, PB, GRADES)
    assert b.id != a.id and b.status == AlertStatus.NEW


@pytest.mark.parametrize("status", ANALYST)
def test_stale_expiry_only_for_new(status: AlertStatus) -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    later = NOW + timedelta(minutes=120)
    assert fuse([], [a.model_copy(update={"status": status})], later, None, PB, GRADES) == []
    (x,) = fuse([], [a], later, None, PB, GRADES)
    assert x.status == AlertStatus.EXPIRED


@pytest.mark.parametrize("status", ANALYST)
def test_apply_evidence_update_preserves_analyst_status(status: AlertStatus) -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    stored = a.model_copy(update={"status": status, "title": "analyst title"})
    upd = a.model_copy(update={"summary": "fresh", "status": AlertStatus.NEW})
    m = apply_evidence_update(stored, upd)
    assert m.status == status and m.title == "analyst title" and m.summary == "fresh"
    exp = apply_evidence_update(stored, a.model_copy(update={"status": AlertStatus.EXPIRED}))
    assert exp.status == status


@pytest.mark.parametrize("status", ANALYST)
def test_pipeline_never_overwrites_analyst_status(status: AlertStatus) -> None:
    store = _store(jammed_cell(n_aircraft=30, n_jammed=10, minutes=45))
    cfg = _settings()
    for m in (35, 40):
        tick(store, cfg, T0 + timedelta(minutes=m), playbook=PB, source_grades=GRADES)
    (a,) = [x for x in store.alerts.values() if x.kind == AlertKind.GNSS_JAMMING]
    store.upsert_alert(a.model_copy(update={"status": status}))
    tick(store, cfg, T0 + timedelta(minutes=45), playbook=PB, source_grades=GRADES)
    jams = [x for x in store.alerts.values() if x.kind == AlertKind.GNSS_JAMMING]
    assert len(jams) == 1 and jams[0].status == status
    assert jams[0].last_seen > a.last_seen


# --------------------------------------------------------------------------- 5. AOI in ids


def test_aoi_in_evidence_and_alert_ids() -> None:
    from heimdall.detect import jamming, orbit, spoofing
    from heimdall.fuse.fusion import alert_id
    from tests.synthetic.generators import circle_orbit, teleport_spoof

    obs, now = jammed_cell(), T0 + timedelta(minutes=30)
    a = jamming.detect(obs, {}, now, None, aoi="myanmar")[0]
    b = jamming.detect(obs, {}, now, None, aoi="south_china_sea")[0]
    assert a.id != b.id and a.aoi == "myanmar"
    s1 = spoofing.detect(teleport_spoof(), {}, now, aoi="x")[0]
    s2 = spoofing.detect(teleport_spoof(), {}, now, aoi="y")[0]
    assert s1.id != s2.id
    cfg, t75 = {"mil_types": ["E3TF"]}, T0 + timedelta(minutes=75)
    o1 = orbit.detect(circle_orbit(), cfg, t75, None, aoi="x")[0]
    o2 = orbit.detect(circle_orbit(), cfg, t75, None, aoi="y")[0]
    assert o1.id != o2.id and o1.aoi == "x"
    assert alert_id("k", "c", now, "x") != alert_id("k", "c", now, "y")
    (al,) = fuse([ev()], [], NOW, None, PB, GRADES, aoi="myanmar")
    assert al.id == alert_id("gnss_jamming", ev().h3_cell, ev().window_start, "myanmar")
