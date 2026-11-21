"""Alert refs, sources and footprint. Offline."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from heimdall import config
from heimdall.adapters.composite_store import CompositeStore
from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_store import MemoryStore
from heimdall.backtest.replay import ReplayStore, replay
from heimdall.backtest.traces import iter_trace_dir
from heimdall.fuse.fusion import fuse
from heimdall.schemas import (
    Admiralty,
    Alert,
    AlertKind,
    AlertStatus,
    Severity,
    SourceReliability,
)
from tests.synthetic import backtest_fixtures as fx
from tests.unit.test_corroborate import JAM_LAT, JAM_LON, mk
from tests.unit.test_fusion import GRADES, NOW, PB, ev
from tests.unit.test_fusion_ais import GR, jam, notices, one

REF = re.compile(r"^HMD-\d{8}-\d{4}$")
D1 = datetime(2026, 9, 30, 23, 50, tzinfo=UTC)


def mk_alert(i: str, first_seen: datetime = D1, **kw: object) -> Alert:
    return Alert(
        id=i,
        kind=AlertKind.GNSS_JAMMING,
        severity=Severity.HIGH,
        confidence=0.9,
        admiralty=Admiralty(reliability=SourceReliability.B, credibility=3),
        title="t",
        summary="s",
        evidence_ids=["e"],
        first_seen=first_seen,
        last_seen=first_seen,
        **kw,  # type: ignore[arg-type]
    )


def _memory(tmp_path: Path) -> object:
    return MemoryStore()


def _duck(tmp_path: Path) -> object:
    return DuckDBStore(tmp_path / "h.duckdb")


def _composite(tmp_path: Path) -> object:
    return CompositeStore(MemoryStore(), MemoryStore())


def _replay(tmp_path: Path) -> object:
    return ReplayStore()


@pytest.fixture(params=[_memory, _duck, _composite, _replay], ids=lambda f: f.__name__[1:])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> object:
    return request.param(tmp_path)


def test_new_alert_gets_ref(store) -> None:  # type: ignore[no-untyped-def]
    store.upsert_alert(mk_alert("a"))
    ref = store.get_alert("a").ref
    assert ref is not None and REF.match(ref) and ref == "HMD-20260930-0001"


def test_reput_and_update_keep_ref(store) -> None:  # type: ignore[no-untyped-def]
    store.upsert_alert(mk_alert("a"))
    store.upsert_alert(mk_alert("b"))
    ref = store.get_alert("a").ref
    store.upsert_alert(mk_alert("a"))  # same id, no ref
    store.upsert_alert(mk_alert("a", ref="HMD-20260930-0099"))  # an incoming ref never replaces
    upd = store.get_alert("a").model_copy(update={"severity": Severity.LOW})
    store.upsert_alert(upd)
    assert store.get_alert("a").ref == ref
    assert store.get_alert("b").ref == "HMD-20260930-0002"  # re-puts did not consume numbers


def test_consecutive_numbers_and_new_day_restarts(store) -> None:  # type: ignore[no-untyped-def]
    store.upsert_alert(mk_alert("a"))
    store.upsert_alert(mk_alert("b"))
    store.upsert_alert(mk_alert("c", D1 + timedelta(minutes=20)))  # next UTC day
    store.upsert_alert(mk_alert("d", D1 + timedelta(minutes=25)))
    got = [store.get_alert(i).ref for i in "abcd"]
    assert got == [
        "HMD-20260930-0001",
        "HMD-20260930-0002",
        "HMD-20261001-0001",
        "HMD-20261001-0002",
    ]


def test_status_change_keeps_ref(store) -> None:  # type: ignore[no-untyped-def]
    store.upsert_alert(mk_alert("a"))
    ref = store.get_alert("a").ref
    store.upsert_alert(mk_alert("a").model_copy(update={"status": AlertStatus.ACKNOWLEDGED}))
    assert store.get_alert("a").ref == ref


def test_legacy_alert_json_without_new_fields_validates() -> None:
    a = mk_alert("old")
    doc = a.model_dump(mode="json", exclude={"ref", "sources", "footprint"})
    b = Alert.model_validate(doc)
    assert b.ref is None and b.sources == [] and b.footprint is None


def test_duckdb_restart_keeps_ref_and_continues(tmp_path: Path) -> None:
    p = tmp_path / "h.duckdb"
    s = DuckDBStore(p)
    s.upsert_alert(mk_alert("a"))
    s.upsert_alert(mk_alert("b"))
    s._con.close()
    s2 = DuckDBStore(p)
    assert s2.get_alert("a").ref == "HMD-20260930-0001"  # type: ignore[union-attr]
    s2.upsert_alert(mk_alert("a"))
    s2.upsert_alert(mk_alert("c"))
    assert s2.get_alert("a").ref == "HMD-20260930-0001"  # type: ignore[union-attr]
    assert s2.get_alert("c").ref == "HMD-20260930-0003"  # type: ignore[union-attr]


def test_duckdb_legacy_table_gets_ref_column(tmp_path: Path) -> None:
    import duckdb

    p = tmp_path / "old.duckdb"
    con = duckdb.connect(str(p))
    con.execute(
        "CREATE TABLE alerts (id VARCHAR PRIMARY KEY, kind VARCHAR, severity VARCHAR,"
        " status VARCHAR, aoi VARCHAR, confidence DOUBLE, first_seen TIMESTAMP,"
        " last_seen TIMESTAMP, doc VARCHAR)"
    )
    con.close()
    s = DuckDBStore(p)
    s.upsert_alert(mk_alert("a"))
    assert s.get_alert("a").ref == "HMD-20260930-0001"  # type: ignore[union-attr]


def test_lake_rebuild_honours_incoming_ref_and_bumps_counter(store) -> None:  # type: ignore[no-untyped-def]
    store.upsert_alert(mk_alert("a", ref="HMD-20260930-0007"))
    store.upsert_alert(mk_alert("b"))
    assert store.get_alert("a").ref == "HMD-20260930-0007"
    assert store.get_alert("b").ref == "HMD-20260930-0008"


def test_replay_refs_are_deterministic() -> None:
    obs = list(iter_trace_dir(fx.ROOT / "traces"))
    end = fx.T_START + timedelta(hours=2.5)
    runs = [replay(obs, config.settings(), "baltic", fx.T_START, end) for _ in range(2)]
    refs = [[(a.id, a.ref) for a in r.alerts] for r in runs]
    assert refs[0] and refs[0] == refs[1]
    assert all(REF.match(str(ref)) for _, ref in refs[0])


# --------------------------------------------------------------------------- fusion


def test_fusion_sources_one_per_connector_with_family_and_grade() -> None:
    n = notices("nga-exercise-1")
    a = one(fuse([jam(score=0.3)], [], NOW, None, PB, GR, notices=n), AlertKind.GNSS_JAMMING)  # type: ignore[list-item]
    by = {s.connector: s for s in a.sources}
    assert set(by) == {"adsb_lol", "nga_msi"} and len(a.sources) == 2
    assert by["adsb_lol"].family == "adsb" and by["nga_msi"].family == "notice"
    assert by["nga_msi"].admiralty.reliability == SourceReliability.A
    assert by["adsb_lol"].admiralty.reliability == SourceReliability(GRADES["adsb_lol"])
    assert all(s.admiralty.credibility == a.admiralty.credibility == 2 for s in a.sources)
    assert a.sources[0].connector == "nga_msi"  # sorted best reliability first


def test_fusion_multiple_mirrors_are_separate_connectors() -> None:
    j = jam(showing=("adsb.lol", "adsb.fi"), covering=("adsb.lol", "adsb.fi"))
    a = one(fuse([j], [], NOW, None, PB, GR), AlertKind.GNSS_JAMMING)  # type: ignore[list-item]
    assert {s.connector for s in a.sources} == {"adsb_lol", "adsb_fi"}
    assert {s.family for s in a.sources} == {"adsb"}


def test_fusion_unknown_source_grades_f() -> None:
    e = ev(showing=("mystery",), covering=("mystery",))
    a = one(fuse([e], [], NOW, None, PB, GR), AlertKind.GNSS_JAMMING)
    (s,) = a.sources
    assert s.connector == "mystery" and s.family == "src:mystery"
    assert s.admiralty.reliability == SourceReliability.F


def test_fusion_jamming_cell_footprint_is_area() -> None:
    a = one(fuse([jam()], [], NOW, None, PB, GR), AlertKind.GNSS_JAMMING)  # type: ignore[list-item]
    assert a.footprint is not None and a.footprint.kind == "area_cell"
    assert a.h3_cell in a.footprint.description and "km" in a.footprint.description


def test_fusion_ais_spoof_single_vessel_is_track_and_multi_is_area() -> None:
    one_v = mk(AlertKind.AIS_SPOOFING, "s1", JAM_LAT, JAM_LON, src="aisstream", entities=["9001"])
    a = one(fuse([one_v], [], NOW, None, PB, GR), AlertKind.AIS_SPOOFING)
    assert a.footprint is not None and a.footprint.kind == "single_track"
    assert "9001" in a.footprint.description
    assert [s.connector for s in a.sources] == ["aisstream"]
    ring = mk(AlertKind.AIS_SPOOFING, "s2", JAM_LAT, JAM_LON, src="aisstream", entities=["1", "2"])
    b = one(fuse([ring], [], NOW, None, PB, GR), AlertKind.AIS_SPOOFING)
    assert b.footprint is not None and b.footprint.kind == "area_cell"


def test_fusion_update_refreshes_sources_and_keeps_stored_ref() -> None:
    st = MemoryStore()
    (a1,) = fuse([jam()], [], NOW, None, PB, GR)  # type: ignore[list-item]
    st.upsert_alert(a1)
    stored = st.get_alert(a1.id)
    assert stored is not None and stored.ref is not None
    n = notices("nga-exercise-1")
    (a2,) = fuse([jam(score=0.3)], [stored], NOW, None, PB, GR, notices=n)  # type: ignore[list-item]
    assert a2.ref == stored.ref and len(a2.sources) == 2
    st.upsert_alert(a2)
    assert st.get_alert(a1.id).ref == stored.ref  # type: ignore[union-attr]


def test_api_alert_json_includes_ref_sources_footprint() -> None:
    from fastapi.testclient import TestClient

    from heimdall.api.app import create_app

    st = MemoryStore()
    (a,) = fuse([jam()], [], NOW, None, PB, GR)  # type: ignore[list-item]
    st.upsert_alert(a)
    c = TestClient(
        create_app(st, playbook={}, allowed_hosts=["testserver"]),
        headers={"Origin": "http://testserver"},
    )
    listed = c.get("/api/alerts").json()[0]
    assert REF.match(listed["ref"]) and listed["footprint"]["kind"] == "area_cell"
    assert listed["sources"][0]["connector"] == "adsb_lol"
    detail = c.get(f"/api/alerts/{a.id}").json()["alert"]
    assert detail["ref"] == listed["ref"]
