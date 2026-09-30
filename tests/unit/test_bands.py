"""028: band tags per detector, fusion union, old rows, coverage API (offline)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.api.traffic import traffic_router
from heimdall.config import AOI, playbook
from heimdall.detect import ais_gap, ais_spoofing, jamming, orbit, spoofing, thermal, warning_area
from heimdall.fuse.fusion import fuse
from heimdall.geo import h3_cell
from heimdall.live import LivePositions
from heimdall.schemas import AircraftObs, Alert, Band, Evidence, GeoEvent, RfEffect, VesselObs
from tests.synthetic import thermal as syn_thermal
from tests.synthetic import vessels as V
from tests.synthetic.generators import (
    T0,
    circle_orbit,
    jammed_cell,
    teleport_spoof,
)
from tests.unit.test_fusion import GRADES, ev
from tests.unit.test_fusion import NOW as FNOW

NOW = T0 + timedelta(minutes=30)
FIXTURE = Path(__file__).parent.parent / "fixtures" / "notices" / "geoevents_p2.json"


def test_schema_defaults_and_old_rows_load() -> None:
    e = ev()
    assert e.bands == [] and e.rf_effects is None
    old = json.loads(e.model_dump_json())
    del old["bands"], old["rf_effects"]
    assert Evidence.model_validate(old).bands == []
    (a,) = fuse([ev()], [], FNOW, None, playbook(), GRADES)
    old_a = json.loads(a.model_dump_json())
    del old_a["bands"]
    assert Alert.model_validate(old_a).bands == []


def test_roundtrip_through_store() -> None:
    st = DuckDBStore(":memory:")
    e = ev().model_copy(
        update={
            "bands": [Band.L1_GNSS],
            "rf_effects": [
                RfEffect(
                    band=Band.L1_GNSS,
                    sensor="aircraft",
                    quantity="nic_loss_ratio",
                    value=0.4,
                    n_receivers=12,
                    coverage=0.8,
                )
            ],
        }
    )
    st.upsert_evidence(e)
    (back,) = st.get_evidence([e.id])
    assert back.bands == [Band.L1_GNSS] and back.rf_effects and back.rf_effects[0].n_receivers == 12


def test_jamming_tag() -> None:
    evs = jamming.detect(jammed_cell(n_aircraft=12, n_jammed=4), {}, NOW, None)
    assert evs and all(e.bands == [Band.L1_GNSS] for e in evs)


def test_spoofing_tag() -> None:
    (e,) = spoofing.detect(teleport_spoof(), {}, NOW)
    assert e.bands == [Band.L1_GNSS]


def test_ais_spoofing_tag() -> None:
    evs = ais_spoofing.detect(V.ring_spoof(), {}, V.NOW, None, aoi="scs")
    assert evs and all(e.bands == [Band.L1_GNSS] for e in evs)


def test_ais_gap_tag() -> None:
    (e,) = ais_gap.detect(V.dark_gap_covered(), {}, V.GAP_NOW, None, aoi="scs")
    assert e.bands == [Band.VHF_MARINE]


def test_orbit_tag() -> None:
    cfg = {"mil_types": ["K35R", "E3TF"], "tanker_types": ["K35R"]}
    (e,) = orbit.detect(circle_orbit(), cfg, T0 + timedelta(minutes=75), None)
    assert e.bands == [Band.NONE]


def test_thermal_tag() -> None:
    ev_, base = syn_thermal.new_cluster()
    aoi = AOI(name="south_china_sea", lat=12.5, lon=113.5, radius_nm=250)
    (e,) = thermal.detect(ev_, base, {}, syn_thermal.NOW, aoi=aoi)
    assert e.bands == [Band.NONE]


def test_warning_area_tag_regex_only() -> None:
    events = [GeoEvent.model_validate(x) for x in json.loads(FIXTURE.read_text())]
    aoi = AOI(name="south_china_sea", lat=12.5, lon=113.5, radius_nm=250)
    by = {e.records[0].id: e.bands for e in warning_area.detect(events, {}, NOW, aoi=aoi)}
    assert by["faa-gnss-1"] == [Band.L1_GNSS]  # category names GNSS
    assert by["nga-exercise-1"] == [Band.NONE]


def test_fusion_union() -> None:
    a = ev(ident="a").model_copy(update={"bands": [Band.L1_GNSS]})
    b = ev(ident="b").model_copy(update={"bands": [Band.NONE, Band.L_1090]})
    (al,) = fuse([a, b], [], FNOW, None, playbook(), GRADES)
    assert al.bands == [Band.L1_GNSS, Band.L_1090]
    (none_only,) = fuse(
        [ev().model_copy(update={"bands": [Band.NONE]})], [], FNOW, None, playbook(), GRADES
    )
    assert none_only.bands == [Band.NONE]


# ---------------------------------------------------------------- coverage API
CNOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _air(hex_: str, lat: float, lon: float, nic: int | None, glob: bool = False) -> AircraftObs:
    return AircraftObs(
        ts=CNOW - timedelta(seconds=10),
        hex=hex_,
        provider="adsb.lol",
        lat=lat,
        lon=lon,
        src_type="adsb_icao",
        nic=nic,
        is_global=glob,
    )


def _client(idx: LivePositions | None) -> TestClient:
    app = FastAPI()
    app.include_router(traffic_router(DuckDBStore(":memory:"), now=lambda: CNOW, index=idx))
    return TestClient(app)


def test_coverage_api() -> None:
    idx = LivePositions()
    idx.update_air(
        [
            _air("aaaaaa", 1.0, 103.0, 8),
            _air("bbbbbb", 1.01, 103.01, 8),
            _air("cccccc", 1.02, 103.0, None),  # no NIC/NACp: not a GNSS receiver
            _air("dddddd", 1.0, 103.0, 8, glob=True),
        ],
        CNOW,
    )
    idx.update_sea(
        [VesselObs(ts=CNOW - timedelta(minutes=20), mmsi=5, provider="aisstream", lat=1, lon=103)],
        CNOW,
    )
    c = _client(idx)
    cell = h3_cell(1.0, 103.0, 4)
    r = c.get("/api/coverage", params={"band": "l1_gnss", "bbox": "100,0,110,5"}).json()
    assert r["source"] == "live" and r["cells"] == [{"h3": cell, "receivers": 2}]
    r = c.get("/api/coverage", params={"band": "vhf_marine"}).json()
    assert r["cells"] == [{"h3": cell, "receivers": 1}]
    assert c.get("/api/coverage", params={"band": "hf"}).json()["source"] == "none"
    assert c.get("/api/coverage", params={"band": "bogus"}).status_code == 422
    r = _client(None).get("/api/coverage", params={"band": "l1_gnss"}).json()
    assert r == {"band": "l1_gnss", "window_s": 3600, "cells": [], "source": "none"}
