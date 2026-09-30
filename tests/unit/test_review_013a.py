"""Regression tests for review round 013a: AIS gap AOI edge, feed outage, A3 ring per
neighbourhood, and plain-language counterfactual/description text."""

from __future__ import annotations

import random
import re
from datetime import timedelta

from heimdall.detect import jamming, orbit, spoofing
from heimdall.detect.ais_gap import detect as gap_detect
from heimdall.detect.ais_gap import find_gaps
from heimdall.detect.ais_spoofing import _ring
from heimdall.detect.ais_spoofing import detect as ais_spoof_detect
from heimdall.geo import within_radius_nm
from heimdall.schemas import Evidence, VesselObs
from tests.synthetic import generators as G
from tests.synthetic import vessels as V

END = V.GAP_NOW
RING_CFG = {
    "ring_min_vessels": 5,
    "ring_cv_max": 0.1,
    "ring_bearing_circvar_min": 0.7,
    "ring_radius_km": [0.2, 30.0],
}


def _in_aoi(lat: float, lon: float) -> bool:
    return within_radius_nm(lat, lon, V.LAT0, V.LON0, 20.0)  # ~37 km


def _outbound_transit(mmsi: int = 222000001) -> list[VesselObs]:
    """Reports every 10 min for 24 h, sailing east at 15 kn from the AOI centre."""
    out = []
    steps = 24 * 6
    for i in range(steps):
        la, lo = G.destination(V.LAT0, V.LON0, 90.0, 15 * 1.852 * i / 6.0)
        out.append(V.make_vessel(mmsi, END - timedelta(minutes=10 * (steps - i)), la, lo, sog=15))
    return out


# ---- 1. AOI edge: ships sailing out of the AOI are not dark ----
def test_ship_leaving_aoi_is_not_a_gap() -> None:
    ship = _outbound_transit()
    edge = G.destination(V.LAT0, V.LON0, 90.0, 30.0)  # covered neighbours near the AOI edge
    allv = ship + V.background_traffic(*edge)
    # old pipeline behaviour (AOI-filtered input) mistook the exit for a dark gap ...
    filtered = [o for o in allv if _in_aoi(o.lat, o.lon)]
    assert any(e.entities == [str(ship[0].mmsi)] for e in gap_detect(filtered, {}, END))
    # ... unfiltered window + assignment by last fix gives nothing
    evs = gap_detect(allv, {}, END, in_region=_in_aoi)
    assert not [e for e in evs if e.entities == [str(ship[0].mmsi)]]


def test_gap_is_assigned_by_last_fix() -> None:
    obs = V.dark_gap_covered()
    assert len(gap_detect(obs, {}, END, in_region=_in_aoi)) == 1
    assert gap_detect(obs, {}, END, in_region=lambda la, lo: False) == []


# ---- 2. feed outage / partial hour ----
def test_global_feed_outage_gives_no_evidence() -> None:
    outage = END - timedelta(hours=2.5)
    obs = V.gap_target(silent_h=5.0) + V.background_traffic(V.LAT0, V.LON0, end=outage, hours=21.5)
    gaps = [g for g in find_gaps(obs, {}, END) if g["mmsi"] == 111000001]
    assert gaps and gaps[0]["covered_frac"] >= 0.5  # coverage alone would have passed
    assert gaps[0]["reason"] == "feed not live"
    assert gap_detect(obs, {}, END) == []


def test_partial_final_hour_counts_as_a_bin() -> None:
    # closed 2.5 h gap; neighbours only heard in the first hour -> 1 of ceil(2.5)=3 bins covered
    last = END - timedelta(hours=3)
    obs = V.gap_target(silent_h=3.0, resume_after_h=2.5) + V.background_traffic(
        V.LAT0, V.LON0, end=last + timedelta(hours=1), hours=24
    )
    (g,) = [g for g in find_gaps(obs, {}, END) if g["mmsi"] == 111000001 and not g["open"]]
    assert abs(g["covered_frac"] - 1 / 3) < 1e-6 and not g["fire"]
    assert gap_detect(obs, {}, END) == []


# ---- 3. A3 ring hidden by AOI-wide background traffic ----
def _background_transits(n: int = 30, near: int = 5) -> list[VesselObs]:
    rng = random.Random(13)
    out: list[VesselObs] = []
    for k in range(n):
        d = rng.uniform(15, 40) if k < near else rng.uniform(150, 350)
        la, lo = G.destination(V.LAT0, V.LON0, rng.uniform(0, 360), d)
        out += V.straight_transit(
            mmsi=210000000 + k, lat=la, lon=lo, bearing=rng.uniform(0, 360), n=30
        )
    return out


def test_ring_found_among_background_traffic() -> None:
    ring, bg = V.ring_spoof(), _background_transits()
    cut = V.T0 + timedelta(seconds=300)
    last = {o.mmsi: o for o in sorted(ring + bg, key=lambda x: x.ts) if o.ts < cut}
    assert _ring(last, RING_CFG) is None  # one AOI-wide centroid (old behaviour) misses it
    evs = ais_spoof_detect(ring + bg, {}, V.NOW, None, aoi="scs")
    rings = [e for e in evs if e.features.get("A3")]
    assert len(rings) == 1 and set(rings[0].entities) == {str(o.mmsi) for o in ring}


def test_background_traffic_alone_is_not_a_ring() -> None:
    evs = ais_spoof_detect(_background_transits(60, near=20), {}, V.NOW, None, aoi="scs")
    assert not [e for e in evs if e.features.get("A3")]


# ---- 4. plain words ----
JARGON = re.compile(r"R'|z-score|robust z|\bMAD\b|\bNIC\b|NACp|\bH3\b|sigma|\bratio\b", re.I)


def _texts(evs: list[Evidence]) -> list[str]:
    return [t for e in evs for t in [e.counterfactual, *e.gaps, *(r.description for r in e.rules)]]


def test_user_facing_text_is_plain() -> None:
    now = G.T0 + timedelta(minutes=30)
    jam = jamming.detect(G.jammed_cell(n_aircraft=12, n_jammed=4), {}, now, None)
    assert jam and "GPS accuracy" in jam[0].counterfactual
    spo = spoofing.detect(G.teleport_spoof(), {}, now)
    onow = G.T0 + timedelta(minutes=75)
    orb = orbit.detect(G.circle_orbit(), {"mil_types": ["K35R"], "tanker_types": []}, onow)
    ais = ais_spoof_detect(V.ring_spoof() + V.circle_track(), {}, V.NOW, None)
    gap = gap_detect(V.dark_gap_covered(), {}, END)
    for evs in (jam, spo, orb, ais, gap):
        assert evs
        bad = [t for t in _texts(evs) if JARGON.search(t)]
        assert not bad, bad
