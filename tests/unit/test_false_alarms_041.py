"""Task 041: false-alarm reductions (equipment faults, minimum degraded count, jammer merge)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from heimdall.api.spectrum import merge_jammers
from heimdall.detect import jamming
from tests.synthetic.generators import T0, jammed_cell, make_obs

NOW = T0 + timedelta(minutes=30)


def _cells(lats: list[float], hex_: str, bad: bool) -> list:
    """One aircraft flying through several cells (one fix per minute in each)."""
    out = []
    for k, la in enumerate(lats):
        t = T0 + timedelta(minutes=5 + 5 * k)
        out.append(make_obs(hex_, t, la, 96.0, nic=0 if bad else 9, nac_p=0 if bad else 10))
    return out


def test_equipment_fault_aircraft_is_excluded() -> None:
    """Degraded in every cell it crosses while its neighbours are healthy -> chronic."""
    lats = [20.0, 20.5, 21.0, 21.5]
    obs = _cells(lats, "faulty", True)
    for n in range(3):
        obs += _cells(lats, f"good{n}", False)
    assert jamming.chronic_aircraft(obs, {}) == {"faulty"}


def test_area_wide_degradation_is_not_chronic() -> None:
    """Everyone degraded together (interference) -> nobody is an equipment fault."""
    lats = [20.0, 20.5, 21.0, 21.5]
    obs = [o for n in range(4) for o in _cells(lats, f"jam{n}", True)]
    assert jamming.chronic_aircraft(obs, {}) == set()


def test_two_degraded_aircraft_do_not_alert() -> None:
    """2 of 9 passes the de-noised ratio (1/9 = 0.11) but not min_degraded (3)."""
    assert jamming.detect(jammed_cell(n_aircraft=9, n_jammed=2), {}, NOW, None) == []
    (e,) = jamming.detect(jammed_cell(n_aircraft=9, n_jammed=3), {}, NOW, None)
    assert {r.rule: r.passed for r in e.rules}["min_degraded"]


def _feat(ref: str, lat: float, lon: float, agreement: float, major: float) -> dict:
    p = {
        "ref": ref,
        "lat": lat,
        "lon": lon,
        "agreement": agreement,
        "major_km": major,
        "last_seen": datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
    }
    return {"type": "Feature", "properties": p}


def test_nearby_jammer_estimates_merge_into_the_best_fit() -> None:
    m = merge_jammers(
        [
            _feat("A", 28.0, 56.5, 0.75, 120),
            _feat("B", 28.3, 56.6, 0.91, 90),  # best fit wins
            _feat("C", 28.5, 56.0, 0.80, 100),
            _feat("D", 25.6, 53.2, 0.72, 121),  # > 150 km away: its own emitter
        ]
    )
    assert [x["properties"]["ref"] for x in m] == ["B", "D"]
    assert (
        sorted(m[0]["properties"]["alerts"]) == ["A", "B", "C"]
        and m[0]["properties"]["n_alerts"] == 3
    )


def test_alerts_of_unmonitored_areas_expire() -> None:
    from heimdall.pipeline import expire_unmonitored
    from heimdall.schemas import Admiralty, Alert, AlertKind, AlertStatus, Severity
    from tests.unit.fakes import FakeStore

    store = FakeStore()
    old = NOW - timedelta(hours=3)

    def al(i: str, aoi: str, status: AlertStatus = AlertStatus.NEW) -> Alert:
        return Alert(
            id=i,
            kind=AlertKind.GNSS_JAMMING,
            severity=Severity.LOW,
            confidence=0.5,
            title="t",
            summary="s",
            admiralty=Admiralty(reliability="B", credibility=3),
            evidence_ids=["e"],
            aoi=aoi,
            status=status,
            first_seen=old,
            last_seen=old,
        )

    for a in (al("a", "gone"), al("b", "kept"), al("c", "gone", AlertStatus.ACKNOWLEDGED)):
        store.upsert_alert(a)
    assert expire_unmonitored(store, {"kept"}, {"theatres": []}, {}, NOW) == 1
    st = {a.id: a.status for a in store.list_alerts(limit=10)}
    assert st == {"a": AlertStatus.EXPIRED, "b": AlertStatus.NEW, "c": AlertStatus.ACKNOWLEDGED}
