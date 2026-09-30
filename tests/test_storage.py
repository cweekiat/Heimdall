from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq

from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_bus import MemoryBus
from heimdall.adapters.parquet_lake import ParquetLake
from heimdall.ports import Bus, Lake, Store
from heimdall.schemas import (
    ActionRequest,
    Admiralty,
    AircraftObs,
    Alert,
    AlertKind,
    AlertStatus,
    Domain,
    Evidence,
    Feedback,
    GeoEvent,
    Severity,
    SourceHealth,
    SourceReliability,
    VesselObs,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _ac(i: int, dt: float = 0, h3: str = "c1") -> AircraftObs:
    return AircraftObs(
        ts=T0 + timedelta(seconds=dt), hex=f"h{i}", provider="adsb.lol", lat=1.0, lon=2.0,
        src_type="adsb_icao", nic=8, mil=True, h3_r4=h3, h3_r6="x",
    )  # fmt: skip


def _ev(id_: str = "e1", cell: str = "c1") -> Evidence:
    return Evidence(
        id=id_, kind=AlertKind.GNSS_JAMMING, detector="jam", detector_version="1",
        window_start=T0, window_end=T0 + timedelta(minutes=30), h3_cell=cell, created_at=T0,
    )  # fmt: skip


def _alert(id_: str = "a1", aoi: str = "baltic", status: AlertStatus = AlertStatus.NEW) -> Alert:
    return Alert(
        id=id_, kind=AlertKind.GNSS_JAMMING, severity=Severity.HIGH, confidence=0.8,
        admiralty=Admiralty(reliability=SourceReliability.B, credibility=2), title="t",
        summary="s", aoi=aoi, evidence_ids=["e1"], first_seen=T0, last_seen=T0, status=status,
    )  # fmt: skip


def test_protocols() -> None:
    assert isinstance(DuckDBStore(), Store)
    assert isinstance(MemoryBus(), Bus)
    assert isinstance(ParquetLake("x"), Lake)


def test_store_observations_roundtrip() -> None:
    s = DuckDBStore()
    assert s.insert_aircraft([_ac(1), _ac(2, 10, "c2"), _ac(3, 4000)]) == 3
    assert s.insert_aircraft([]) == 0
    w = s.aircraft_window(T0, T0 + timedelta(hours=1))
    assert [a.hex for a in w] == ["h1", "h2"]
    assert w[0] == _ac(1) and w[0].ts.tzinfo is not None
    assert [a.hex for a in s.aircraft_window(T0, T0 + timedelta(hours=1), ["c2"])] == ["h2"]
    assert s.aircraft_window(T0, T0 + timedelta(hours=1), []) == []

    v = VesselObs(ts=T0, mmsi=123, provider="ais", lat=1, lon=2, sog=5.5, h3_r4="c1")
    assert s.insert_vessels([v]) == 1
    assert s.vessels_window(T0, T0 + timedelta(minutes=1), ["c1"]) == [v]

    e = GeoEvent(
        id="g1", ts=T0, source="firms", domain=Domain.SPACE, kind="thermal", lat=1, lon=2,
        geometry={"type": "Point", "coordinates": [2, 1]}, attrs={"frp": 3}, value=3.0,
    )  # fmt: skip
    assert s.insert_events([e]) == 1
    assert s.events_window(T0, T0 + timedelta(minutes=1), ["thermal"]) == [e]
    assert s.events_window(T0, T0 + timedelta(minutes=1), ["news"]) == []


def test_store_evidence_alerts() -> None:
    s = DuckDBStore()
    s.upsert_evidence(_ev("e1"))
    s.upsert_evidence(_ev("e1"))  # idempotent
    s.upsert_evidence(_ev("e2"))
    assert [e.id for e in s.get_evidence(["e2", "e1", "nope"])] == ["e2", "e1"]
    assert s.get_evidence([]) == []
    hist = s.evidence_history("c1", "gnss_jamming", T0)
    assert len(hist) == 2
    assert s.evidence_history("c1", "gnss_jamming", T0 + timedelta(days=1)) == []
    assert s.evidence_history("zz", "gnss_jamming", T0) == []

    s.upsert_alert(_alert("a1"))
    s.upsert_alert(_alert("a2", aoi="myanmar", status=AlertStatus.CONFIRMED))
    s.upsert_alert(_alert("a1", status=AlertStatus.DISMISSED))
    got = s.get_alert("a1")
    assert got is not None and got.ref is not None  # ADR-006: assigned on first put
    assert got.model_copy(update={"ref": None}) == _alert("a1", status=AlertStatus.DISMISSED)
    assert s.get_alert("zz") is None
    assert len(s.list_alerts()) == 2
    assert [a.id for a in s.list_alerts(aoi="myanmar")] == ["a2"]
    assert [a.id for a in s.list_alerts(status=[AlertStatus.DISMISSED])] == ["a1"]
    assert s.list_alerts(since=T0 + timedelta(days=1)) == []
    assert len(s.list_alerts(limit=1)) == 1


def test_store_analyst_metrics_health() -> None:
    s = DuckDBStore()
    fb = Feedback(alert_id="a1", verdict="dismiss", reason="duplicate", ts=T0)
    s.add_feedback(fb)
    assert s.feedback_for("a1") == [fb] and s.feedback_for("x") == []

    act = ActionRequest(id="x1", alert_id="a1", action_id="notify", params={"k": 1}, ts=T0)
    s.upsert_action(act)
    done = act.model_copy(update={"status": "done", "result": {"ok": True}})
    s.upsert_action(done)
    assert s.actions_for("a1") == [done]

    s.record_metric("c1", "ratio", T0, 0.1)
    s.record_metric("c1", "ratio", T0, 0.2)  # replaces
    s.record_metric("c1", "ratio", T0 + timedelta(hours=1), 0.3)
    assert s.metric_history("c1", "ratio", T0) == [(T0, 0.2), (T0 + timedelta(hours=1), 0.3)]
    assert s.metric_history("c1", "ratio", T0 + timedelta(days=1)) == []

    h = SourceHealth(
        source="adsb_lol", domain=Domain.AIR, reliability=SourceReliability.B, last_ok=T0
    )
    s.upsert_health(h)
    s.upsert_health(h.model_copy(update={"records_last_hour": 5}))
    assert [x.records_last_hour for x in s.list_health()] == [5]


def test_store_read_only(tmp_path: Path) -> None:
    p = tmp_path / "h.duckdb"
    w = DuckDBStore(p)
    w.insert_aircraft([_ac(1)])
    w.close()
    r = DuckDBStore(p, read_only=True)
    assert len(r.aircraft_window(T0, T0 + timedelta(hours=1))) == 1
    r.close()


def test_lake_write_and_paths(tmp_path: Path) -> None:
    lake = ParquetLake(tmp_path)
    rows = [_ac(1), _ac(2, 3700), _ac(3, 1)]
    assert lake.append("aircraft", rows) == 3
    assert lake.append("aircraft", []) == 0
    p = lake.paths("aircraft", T0, T0 + timedelta(minutes=10))
    assert len(p) == 1 and "date=2026-01-01/hour=12/part-" in p[0]
    assert pq.read_table(p[0]).num_rows == 2
    assert len(lake.paths("aircraft", T0, T0 + timedelta(hours=2))) == 2
    assert lake.paths("aircraft", T0 + timedelta(days=2), T0 + timedelta(days=3)) == []
    assert lake.paths("vessels", T0, T0 + timedelta(hours=2)) == []


async def test_memory_bus_fanout_and_drop_oldest() -> None:
    bus = MemoryBus(maxsize=2)
    a = bus.subscribe("t")
    b = bus.subscribe("t")
    for i in range(3):
        await bus.publish("t", [_ac(i)])
    assert bus.dropped["t"] == 2  # two subscribers each dropped one
    got_a = [(await a.__anext__())[0].hex for _ in range(2)]  # type: ignore[attr-defined]
    got_b = [(await b.__anext__())[0].hex for _ in range(2)]  # type: ignore[attr-defined]
    assert got_a == got_b == ["h1", "h2"]
    await bus.close()
    assert [x async for x in a] == []


async def test_memory_bus_topics_isolated() -> None:
    bus = MemoryBus()
    a = bus.subscribe("x")
    await bus.publish("y", [_ac(1)])
    await bus.publish("x", [_ac(2)])
    first = await asyncio.wait_for(a.__anext__(), 1)
    assert first[0].hex == "h2"  # type: ignore[attr-defined]
    await bus.close()


def test_actions_distinct_ids_and_get_action() -> None:
    s = DuckDBStore()
    a = ActionRequest(id="i1", alert_id="a1", action_id="notify", ts=T0)
    b = ActionRequest(id="i2", alert_id="a1", action_id="notify", ts=T0 + timedelta(seconds=1))
    s.upsert_action(a)
    s.upsert_action(b)
    assert [x.id for x in s.actions_for("a1")] == ["i1", "i2"]
    assert s.get_action("i2") == b and s.get_action("nope") is None
    done = a.model_copy(update={"status": "done", "result": {"ok": 1}})
    s.upsert_action(done)
    assert s.get_action("i1") == done and len(s.actions_for("a1")) == 2


def test_notes_roundtrip() -> None:
    from datetime import UTC, datetime

    from heimdall.adapters.duckdb_store import DuckDBStore
    from heimdall.schemas import AlertStatus, AnalystNote

    st = DuckDBStore()
    t = datetime(2026, 9, 30, tzinfo=UTC)
    st.add_note(AnalystNote(id="n1", alert_id="a1", ts=t, text="checked NOTAMs"))
    st.add_note(
        AnalystNote(id="n2", alert_id="a1", ts=t, kind="status", status_to=AlertStatus.ACKNOWLEDGED)
    )
    notes = st.notes_for("a1")
    assert [n.id for n in notes] == ["n1", "n2"]
    assert notes[1].status_to == AlertStatus.ACKNOWLEDGED
    assert st.notes_for("other") == []
