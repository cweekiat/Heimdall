"""Pass prediction from the hand-built Sentinel-2A-like OMM fixture (task 020)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from heimdall.orbits import passes, satrec_from_omm

OMM = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "celestrak" / "gp_sentinel2a.json").read_text()
)[0]
POINT = (20.5, 96.5)
START = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
# Reference: same elements propagated at 1 s resolution over 48 h (scratch script), >= 20 deg.
REF = [
    (datetime(2025, 6, 1, 19, 38, 9, tzinfo=UTC), 73.519),
    (datetime(2025, 6, 2, 6, 44, 47, tzinfo=UTC), 34.933),
    (datetime(2025, 6, 2, 19, 8, 19, tzinfo=UTC), 32.762),
    (datetime(2025, 6, 3, 7, 54, 15, tzinfo=UTC), 36.480),
]


def test_passes_match_reference() -> None:
    found = passes(OMM, POINT, START, 48, 20, aoi="myanmar", sensor="MSI")
    assert len(found) == len(REF)
    for ev, (t, el) in zip(found, REF, strict=True):
        assert abs((ev.ts - t).total_seconds()) <= 60
        assert ev.attrs["max_elev_deg"] == pytest.approx(el, abs=1.0)
        assert ev.value == ev.attrs["max_elev_deg"]
        assert ev.kind == "overpass" and ev.source == "celestrak"
        assert ev.attrs["role"] == "context"
        assert ev.attrs["sensor"] == "MSI" and ev.attrs["aoi"] == "myanmar"
        assert ev.attrs["norad_id"] == 40697 and ev.attrs["sat_name"] == "SENTINEL-2A"
        assert ev.valid_from is not None and ev.valid_to is not None
        assert ev.valid_from <= ev.ts <= ev.valid_to
    assert len({e.id for e in found}) == len(found)


def test_min_elevation_filters_low_passes() -> None:
    for thr in (5, 20, 60):
        found = passes(OMM, POINT, START, 48, thr)
        assert all(e.attrs["max_elev_deg"] >= thr for e in found)
    assert len(passes(OMM, POINT, START, 48, 5)) > len(passes(OMM, POINT, START, 48, 20))
    assert [round(e.attrs["max_elev_deg"]) for e in passes(OMM, POINT, START, 48, 60)] == [74]
    assert passes(OMM, POINT, START, 48, 89.9) == []


def test_invalid_omm_raises() -> None:
    bad = {k: v for k, v in OMM.items() if k != "MEAN_MOTION"}
    with pytest.raises(ValueError):
        satrec_from_omm(bad)
    with pytest.raises(ValueError):
        passes({**OMM, "EPOCH": "not-a-date"}, POINT, START, 1, 20)
