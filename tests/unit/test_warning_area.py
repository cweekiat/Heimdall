from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from heimdall.config import AOI
from heimdall.detect.warning_area import detect, is_active, safe_ref
from heimdall.schemas import AlertKind, GeoEvent
from tests.synthetic.generators import T0

FIXTURE = Path(__file__).parent.parent / "fixtures" / "notices" / "geoevents_p2.json"
NOW = T0 + timedelta(minutes=30)
SCS = AOI(name="south_china_sea", lat=12.5, lon=113.5, radius_nm=250)
BALTIC = AOI(name="baltic", role="control", lat=56.0, lon=20.5, radius_nm=250)


def events() -> list[GeoEvent]:
    return [GeoEvent.model_validate(x) for x in json.loads(FIXTURE.read_text())]


def test_only_active_configured_notices_in_aoi() -> None:
    evs = detect(events(), {}, NOW, aoi=SCS)
    assert {e.records[0].id for e in evs} == {"nga-exercise-1", "faa-gnss-1"}
    for e in evs:
        assert e.kind == AlertKind.WARNING_AREA and e.detector_version == "warning-area-1.1.0"
        assert e.aoi == "south_china_sea" and e.counterfactual and e.h3_cell
        assert all(r.passed for r in e.rules)


def test_cancelled_expired_hazard_and_out_of_aoi_excluded() -> None:
    ids = {e.records[0].id for e in detect(events(), {}, NOW, aoi=SCS)}
    assert not ids & {"nga-cancelled-1", "nga-expired-1", "nga-hazard-1", "nga-baltic-1"}
    assert [e.records[0].id for e in detect(events(), {}, NOW, aoi=BALTIC)] == ["nga-baltic-1"]


def test_not_yet_valid_and_expired_by_time() -> None:
    by = {e.id: e for e in events()}
    n = by["nga-exercise-1"]
    assert is_active(n, NOW)
    assert not is_active(n, n.valid_to + timedelta(seconds=1))  # type: ignore[operator]
    assert not is_active(n, n.valid_from - timedelta(hours=1))  # type: ignore[operator]
    assert not is_active(by["gfw-gap-1"], NOW)  # not an area notice


def test_no_untrusted_text_in_evidence() -> None:
    evs = detect(events(), {}, NOW, aoi=SCS)
    blob = json.dumps([e.model_dump(mode="json") for e in evs])
    assert "IGNORE ALL PREVIOUS" not in blob and "<script>" not in blob
    assert any(e.entities == ["HYDROPAC 1234/26"] for e in evs)


def test_ref_is_sanitised() -> None:
    e = events()[0].model_copy(
        update={"attrs": {"ref": "ab<script>1/26; DROP", "status": "active"}}
    )
    assert safe_ref(e) == "ABSCRIPT1/26 DROP"  # only [A-Z0-9/ -] survives


def test_partial_geometry_reduces_confidence() -> None:
    e = events()[0].model_copy(update={"attrs": {**events()[0].attrs, "parse_status": "too_large"}})
    (ev,) = detect([e], {}, NOW, aoi=SCS)
    assert ev.coverage.confidence < 1.0 and ev.gaps
