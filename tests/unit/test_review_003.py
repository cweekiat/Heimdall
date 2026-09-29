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


# --------------------------------------------------------------------------- 4. metric idempotency


def test_rerun_tick_does_not_duplicate_metrics() -> None:
    from tests.unit.test_fusion import CELL

    store = _store(jammed_cell(n_aircraft=30, n_jammed=10, minutes=40))
    cfg = _settings()
    now = T0 + timedelta(minutes=35)
    tick(store, cfg, now, playbook=PB, source_grades=GRADES)
    before = {k: dict(v) for k, v in store.metrics.items()}
    tick(store, cfg, now, playbook=PB, source_grades=GRADES)
    assert {k: dict(v) for k, v in store.metrics.items()} == before
    assert len(store.metric_history(CELL, "jam_ratio", T0)) == 1


def test_rerun_tick_idempotent_on_duckdb_store() -> None:
    from heimdall.adapters.duckdb_store import DuckDBStore
    from tests.unit.test_fusion import CELL

    store = DuckDBStore(":memory:")
    store.insert_aircraft(jammed_cell(n_aircraft=30, n_jammed=10, minutes=45))
    cfg = _settings()
    for m in (35, 40, 40):
        tick(store, cfg, T0 + timedelta(minutes=m), playbook=PB, source_grades=GRADES)
    assert len(store.metric_history(CELL, "jam_ratio", T0)) == 2
    jams = [a for a in store.list_alerts() if a.kind == AlertKind.GNSS_JAMMING]
    assert len(jams) == 1


# --------------------------------------------------------------------------- 2. persistence


def test_single_5min_event_never_reaches_persistence_2() -> None:
    from tests.unit.test_fusion import CELL

    background = jammed_cell(n_aircraft=30, n_jammed=0, minutes=60, seed=1)
    burst = jammed_cell(
        start=T0 + timedelta(minutes=12),
        n_aircraft=6,
        n_jammed=6,
        minutes=3,
        seed=9,
        hex_prefix="j",
    )  # every degraded report falls inside a < 5-min span
    store = _store(background, burst)
    cfg = _settings()
    seen = []
    for m in range(20, 50, 5):  # every window containing the burst re-triggers
        r = tick(store, cfg, T0 + timedelta(minutes=m), playbook=PB, source_grades=GRADES)
        seen += [e for e in r.evidence if e.kind == AlertKind.GNSS_JAMMING and e.h3_cell == CELL]
    assert len(seen) >= 3  # the window-level trigger fires repeatedly ...
    assert max(e.persistence for e in seen) == 1  # ... but it is one short event
    assert not [a for a in store.alerts.values() if a.kind == AlertKind.GNSS_JAMMING]


def test_sustained_event_reaches_persistence_2_on_second_tick() -> None:
    from tests.unit.test_fusion import CELL

    store = _store(jammed_cell(n_aircraft=30, n_jammed=10, minutes=45))
    cfg = _settings()
    ps = []
    for m in (35, 40):
        r = tick(store, cfg, T0 + timedelta(minutes=m), playbook=PB, source_grades=GRADES)
        ps += [e.persistence for e in r.evidence if e.h3_cell == CELL]
    assert ps == [1, 2]


# --------------------------------------------------------------------------- 6. orbit by hex


def test_orbit_persistence_follows_aircraft_across_cells() -> None:
    from tests.synthetic.generators import circle_orbit, destination

    cfg = _settings()
    store = _store(circle_orbit(hex_="ae0777", lat=20.0, lon=96.0))
    r1 = tick(store, cfg, T0 + timedelta(minutes=75), playbook=PB, source_grades=GRADES)
    (o1,) = [e for e in r1.evidence if e.kind == AlertKind.MIL_ORBIT]
    la, lo = destination(20.0, 96.0, 90.0, 80.0)  # centroid moves ~80 km -> another r4 cell
    store.aircraft = circle_orbit(hex_="ae0777", start=T0 + timedelta(minutes=5), lat=la, lon=lo)
    r2 = tick(store, cfg, T0 + timedelta(minutes=80), playbook=PB, source_grades=GRADES)
    (o2,) = [e for e in r2.evidence if e.kind == AlertKind.MIL_ORBIT]
    assert o1.h3_cell != o2.h3_cell
    assert (o1.persistence, o2.persistence) == (1, 2)
    # a different aircraft in the second cell starts its own run
    store.aircraft = circle_orbit(hex_="ae0888", start=T0 + timedelta(minutes=10), lat=la, lon=lo)
    r3 = tick(store, cfg, T0 + timedelta(minutes=85), playbook=PB, source_grades=GRADES)
    (o3,) = [e for e in r3.evidence if e.kind == AlertKind.MIL_ORBIT]
    assert o3.persistence == 1
