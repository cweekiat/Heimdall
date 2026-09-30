"""Regression tests for review follow-ups carried into task 006 (002 writer, 003 fusion/spoofing,
triage journal ordering and the Store status port)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from heimdall.adapters.composite_store import CompositeStore
from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_bus import MemoryBus
from heimdall.adapters.memory_store import MemoryStore
from heimdall.detect import spoofing
from heimdall.fuse.fusion import apply_evidence_update, fuse
from heimdall.run import Writer
from heimdall.schemas import AircraftObs, AlertStatus, AnalystNote
from tests.synthetic.generators import T0, destination, make_obs, straight_flight
from tests.unit.test_fusion import GRADES, NOW, PB, ev

# --------------------------------------------------------------------------- 002 writer


class _Lake:
    def __init__(self) -> None:
        self.n = 0

    def append(self, dataset: str, rows: Any) -> int:
        self.n += len(rows)
        return len(rows)

    def paths(self, dataset: str, start: datetime, end: datetime) -> list[str]:
        return []


def _ac(hex_: str, ts: datetime, provider: str = "adsb.lol") -> AircraftObs:
    return AircraftObs(
        ts=ts, hex=hex_, provider=provider, lat=10.0, lon=100.0, src_type="adsb_icao"
    )


def test_writer_dedupes_replayed_old_observations_across_flushes() -> None:
    """Replay: observation times are a year old; expiry must use the insertion clock."""
    old = datetime(2025, 1, 1, 12, tzinfo=UTC)
    wall = datetime(2026, 9, 30, 12, tzinfo=UTC)
    w = Writer(MemoryBus(), _Lake(), MemoryStore(), ttl_s=30)
    w.buf = [_ac("h1", old)]
    assert w.flush(wall) == 1
    w.buf = [_ac("h1", old + timedelta(seconds=1), "adsb.fi")]
    assert w.flush(wall + timedelta(seconds=5)) == 0  # still deduped (key alive by insertion)
    assert w.providers["h1"] == {"adsb.lol", "adsb.fi"}
    assert w.flush(wall + timedelta(seconds=120)) == 0
    assert w.providers == {}  # expired by insertion time


def test_writer_add_and_flush_are_thread_safe() -> None:
    store = MemoryStore()
    w = Writer(MemoryBus(), _Lake(), store, ttl_s=3600)
    n_threads, per = 4, 250

    def producer(t: int) -> None:
        for i in range(per):
            w.add([_ac(f"t{t}", T0 + timedelta(seconds=10 * i))])

    def flusher(stop: threading.Event) -> None:
        while not stop.is_set():
            w.flush(T0)
            _ = w.providers

    stop = threading.Event()
    f = threading.Thread(target=flusher, args=(stop,))
    f.start()
    ps = [threading.Thread(target=producer, args=(t,)) for t in range(n_threads)]
    for p in ps:
        p.start()
    for p in ps:
        p.join()
    stop.set()
    f.join()
    w.flush(T0)
    assert len(store.aircraft) == n_threads * per  # nothing lost, nothing duplicated


# --------------------------------------------------------------------------- 003 fusion


def test_dismissal_race_escalation_opens_new_alert() -> None:
    """Dismissed between fuse and upsert, and the update escalates: not dropped."""
    (a,) = fuse([ev(score=0.5, conf=0.5)], [], NOW, None, PB, GRADES)
    assert a.severity == "low"
    (upd,) = fuse(
        [ev(ident="e2", end_offset_min=5, score=1.0, conf=0.9)], [a], NOW, None, PB, GRADES
    )
    assert upd.id == a.id and upd.severity == "high"
    dismissed = a.model_copy(update={"status": AlertStatus.DISMISSED})
    out = apply_evidence_update(dismissed, upd)
    assert out.id != a.id and out.status == AlertStatus.NEW and out.severity == "high"
    assert out.evidence_ids == ["e2"] and out.first_seen == upd.last_seen
    # the new alert is picked up as the live alert on the next tick (no duplicate)
    (nxt,) = fuse([ev(ident="e3", end_offset_min=10)], [dismissed, out], NOW, None, PB, GRADES)
    assert nxt.id == out.id


def test_dismissal_race_same_severity_is_bookkeeping_only() -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    (upd,) = fuse([ev(ident="e2", end_offset_min=5)], [a], NOW, None, PB, GRADES)
    dismissed = a.model_copy(update={"status": AlertStatus.DISMISSED})
    out = apply_evidence_update(dismissed, upd)
    assert out.id == a.id and out.status == AlertStatus.DISMISSED
    assert out.evidence_ids == ["e1", "e2"] and out.summary == a.summary


def test_dismissed_campaign_realerts_after_max_episode() -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    # a campaign that kept running (last_seen fresh) since first_seen 13 h ago
    gone = a.model_copy(
        update={
            "status": AlertStatus.DISMISSED,
            "first_seen": NOW - timedelta(hours=13),
            "last_seen": NOW,
        }
    )
    (b,) = fuse([ev(ident="e2", end_offset_min=5)], [gone], NOW, None, PB, GRADES)
    assert b.id != a.id and b.status == AlertStatus.NEW  # default dismiss_max_episode_h = 12
    (c,) = fuse(
        [ev(ident="e2", end_offset_min=5)], [gone], NOW, {"dismiss_max_episode_h": 24}, PB, GRADES
    )
    assert c.id == a.id and c.status == AlertStatus.DISMISSED  # still owned within 24 h


# --------------------------------------------------------------------------- 003 spoofing S1


def _shift(
    obs: list[AircraftObs], after: datetime, km: float, until: datetime | None = None
) -> list[AircraftObs]:
    out = []
    for o in obs:
        if o.ts >= after and (until is None or o.ts < until):
            la, lo = destination(o.lat, o.lon, 0.0, km)
            rest = o.model_dump(exclude={"ts", "hex", "lat", "lon", "h3_r4", "h3_r6"})
            o = make_obs(o.hex, o.ts, la, lo, **rest)
        out.append(o)
    return out


def test_s1_back_to_back_further_jump_is_not_taken_as_return() -> None:
    """Out 150 km at 10 min, then a *further* 150 km at 12 min (no return). The alternation
    assumption would close the capture at the second jump and miss the fixes after it."""
    base = straight_flight("c00009", minutes=20.0)
    obs = _shift(base, T0 + timedelta(minutes=10), 150.0)
    obs = _shift(obs, T0 + timedelta(minutes=12), 150.0)
    s = spoofing.aircraft_signals(obs, {}, T0 + timedelta(minutes=30))["c00009"]
    assert s.fired["S1"]
    after_second = [o.ts for o in s.anomalous if o.ts >= T0 + timedelta(minutes=12)]
    assert after_second, "fixes after the further jump must be anomalous"
    assert min(after_second) == T0 + timedelta(minutes=12)
    # each jump is capped separately (< 300 s from its own jump)
    assert max(after_second) < T0 + timedelta(minutes=17)


def test_s1_out_further_back_closes_on_return() -> None:
    """Out at 10, further at 12, back to the true track at 14: nothing after 14 is anomalous."""
    base = straight_flight("c0000a", minutes=20.0)
    obs = _shift(base, T0 + timedelta(minutes=10), 150.0, T0 + timedelta(minutes=14))
    obs = _shift(obs, T0 + timedelta(minutes=12), 150.0, T0 + timedelta(minutes=14))
    s = spoofing.aircraft_signals(obs, {}, T0 + timedelta(minutes=30))["c0000a"]
    assert s.fired["S1"]
    ts = [o.ts for o in s.anomalous]
    assert min(ts) == T0 + timedelta(minutes=10)
    assert max(ts) < T0 + timedelta(minutes=14)
    assert any(t >= T0 + timedelta(minutes=12) for t in ts)


# --------------------------------------------------------------------------- triage journal


def _note(i: str, ts: datetime, status: AlertStatus | None = None) -> AnalystNote:
    return AnalystNote(
        id=i, alert_id="a1", ts=ts, kind="status" if status else "note", status_to=status
    )


def test_notes_ordered_by_insertion_not_clock() -> None:
    later, earlier = T0 + timedelta(minutes=5), T0
    for st in (MemoryStore(), DuckDBStore()):
        st.add_note(_note("n1", later))  # writer with a fast clock
        st.add_note(_note("n2", earlier))  # written after, but its clock is behind
        st.add_note(_note("n1", later))  # re-add keeps the original slot
        assert [n.id for n in st.notes_for("a1")] == ["n1", "n2"], type(st).__name__


def test_composite_override_uses_latest_inserted_status_note() -> None:
    main, analyst = MemoryStore(), MemoryStore()
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    main.upsert_alert(a.model_copy(update={"id": "a1"}))
    analyst.add_note(_note("s1", T0 + timedelta(minutes=5), AlertStatus.ACKNOWLEDGED))
    analyst.add_note(_note("s2", T0, AlertStatus.UNDER_REVIEW))  # later insert, older clock
    comp = CompositeStore(main, analyst)
    got = comp.get_alert("a1")
    assert got is not None and got.status == AlertStatus.UNDER_REVIEW


def test_record_status_change_port() -> None:
    (a,) = fuse([ev()], [], NOW, None, PB, GRADES)
    a = a.model_copy(update={"id": "a1"})
    note = _note("s1", T0, AlertStatus.ACKNOWLEDGED)
    for st in (MemoryStore(), DuckDBStore()):
        st.upsert_alert(a)
        st.record_status_change(note)
        got = st.get_alert("a1")
        assert got is not None and got.status == AlertStatus.ACKNOWLEDGED
        assert [n.id for n in st.notes_for("a1")] == ["s1"]
    main, analyst = MemoryStore(), MemoryStore()
    main.upsert_alert(a)
    comp = CompositeStore(main, analyst)
    comp.record_status_change(note)
    assert main.alerts["a1"].status == AlertStatus.NEW  # main store never written by analyst
    got = comp.get_alert("a1")
    assert got is not None and got.status == AlertStatus.ACKNOWLEDGED
