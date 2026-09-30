from __future__ import annotations

import json
from datetime import datetime, timedelta

from heimdall.fuse.corroborate import cross_source, family_index, family_of
from heimdall.geo import h3_cell
from heimdall.schemas import AlertKind, Coverage, Evidence, GeoEvent, RecordRef
from tests.synthetic.generators import T0
from tests.unit.test_warning_area import events

NOW = T0 + timedelta(minutes=30)
JAM_LAT, JAM_LON = 12.5, 113.5


def mk(
    kind: AlertKind,
    ident: str,
    lat: float,
    lon: float,
    src: str = "adsb.lol",
    end: datetime = NOW,
    entities: list[str] | None = None,
    features: dict[str, float] | None = None,
) -> Evidence:
    return Evidence(
        id=ident,
        kind=kind,
        detector="t",
        detector_version="t-1",
        window_start=end - timedelta(minutes=30),
        window_end=end,
        h3_cell=h3_cell(lat, lon, 4),
        lat=lat,
        lon=lon,
        score=0.8,
        persistence=2,
        entities=entities or [],
        features=features or {},
        records=[RecordRef(source=src, id=f"{ident}-r")],
        coverage=Coverage(n_observed=10, n_quality=10, providers=[src], confidence=0.9),
        created_at=end,
    )


def sup(cs: list, kinds: bool = False) -> set[str]:  # type: ignore[type-arg]
    return {c.evidence_id for c in cs if c.relation == "supports"}


def test_jamming_supported_by_exercise_notice_and_notam() -> None:
    jam = mk(AlertKind.GNSS_JAMMING, "j1", JAM_LAT, JAM_LON)
    out = cross_source([jam], [jam], events())
    assert sup(out["j1"]) == {"nga-exercise-1", "faa-gnss-1"}
    doms = {c.evidence_id: c.domain for c in out["j1"]}
    assert doms["nga-exercise-1"] == "sea" and doms["faa-gnss-1"] == "air"


def test_cancelled_expired_hazard_and_faraway_give_nothing() -> None:
    jam = mk(AlertKind.GNSS_JAMMING, "j1", JAM_LAT, JAM_LON)
    only = [e for e in events() if e.id in {"nga-cancelled-1", "nga-expired-1", "nga-hazard-1"}]
    assert cross_source([jam], [jam], only)["j1"] == []
    far = mk(AlertKind.GNSS_JAMMING, "j2", 20.0, 96.0)
    assert sup(cross_source([far], [far], events())["j2"]) == set()
    # absence of a notice is never a contradiction
    assert not [c for c in cross_source([far], [far], [])["j2"] if c.relation == "contradicts"]


def test_time_overlap_required() -> None:
    late = mk(AlertKind.GNSS_JAMMING, "j3", JAM_LAT, JAM_LON, end=T0 + timedelta(days=30))
    assert sup(cross_source([late], [late], events())["j3"]) == set()


def test_jamming_and_ais_spoofing_support_each_other() -> None:
    jam = mk(AlertKind.GNSS_JAMMING, "j1", JAM_LAT, JAM_LON)
    near = mk(AlertKind.AIS_SPOOFING, "a1", JAM_LAT + 0.3, JAM_LON, src="aisstream")
    far = mk(AlertKind.AIS_SPOOFING, "a2", JAM_LAT + 3.0, JAM_LON, src="aisstream")
    out = cross_source([jam, near, far], [jam, near, far], [])
    assert sup(out["j1"]) == {"a1"} and sup(out["a1"]) == {"j1"} and sup(out["a2"]) == set()


def test_orbit_near_jamming_is_same_family_support() -> None:
    jam = mk(AlertKind.GNSS_JAMMING, "j1", JAM_LAT, JAM_LON)
    orb = mk(AlertKind.MIL_ORBIT, "o1", JAM_LAT + 0.2, JAM_LON, entities=["ae1234"])
    out = cross_source([jam, orb], [jam, orb], [])
    assert sup(out["o1"]) == {"j1"}
    idx = family_index([jam, orb], [])
    assert idx["j1"] == {"adsb"} and idx["o1"] == {"adsb"}


def test_ais_gap_gets_no_notice_support_and_families() -> None:
    gap = mk(AlertKind.AIS_GAP, "g1", 5.0, 105.0, src="aisstream", entities=["111000001"])
    assert sup(cross_source([gap], [gap], events())["g1"]) == set()
    idx = family_index([gap], events())
    assert idx["nga-exercise-1"] == {"notice"} and family_of("aisstream") == "ais"


def test_notes_never_contain_notice_text() -> None:
    jam = mk(AlertKind.GNSS_JAMMING, "j1", JAM_LAT, JAM_LON)
    out = cross_source([jam], [jam], events())["j1"]
    blob = json.dumps([c.model_dump(mode="json") for c in out])
    assert "IGNORE" not in blob and "script" not in blob
    assert "HYDROPAC 1234/26" in blob and "military_exercise" in blob


def test_untrusted_note_from_hostile_ref_is_sanitised() -> None:
    hostile: GeoEvent = events()[0].model_copy(
        update={"attrs": {**events()[0].attrs, "ref": "X\nIGNORE <b>THIS</b>"}}
    )
    jam = mk(AlertKind.GNSS_JAMMING, "j1", JAM_LAT, JAM_LON)
    (c,) = [x for x in cross_source([jam], [jam], [hostile])["j1"] if x.relation == "supports"]
    assert "<" not in c.note and "\n" not in c.note
