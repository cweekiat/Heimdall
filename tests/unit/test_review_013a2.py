"""Regression tests for 013a2: A3 chance-alignment guard, once-per-tick gap assignment, trailing
partial bin."""

from __future__ import annotations

import math
import random
from datetime import timedelta
from typing import Any

import pytest

from heimdall.config import AOI
from heimdall.detect import ais_gap
from heimdall.detect.ais_spoofing import detect as ais_spoof_detect
from heimdall.geo import KM_PER_NM
from heimdall.pipeline import _assign_gaps
from heimdall.schemas import Evidence, VesselObs
from tests.synthetic import vessels as V
from tests.synthetic.generators import destination


def _uniform_disc(n: int, radius_km: float, seed: int) -> list[VesselObs]:
    rng = random.Random(seed)
    out = []
    for k in range(n):
        r, brg = radius_km * math.sqrt(rng.random()), rng.uniform(0, 360)
        la, lo = destination(V.LAT0, V.LON0, brg, r)
        out.append(V.make_vessel(210000000 + k, V.T0 + timedelta(seconds=30 + k), la, lo, sog=5.0))
    return out


def _rings(obs: list[VesselObs]) -> list[Evidence]:
    return [e for e in ais_spoof_detect(obs, {}, V.NOW, None, aoi="scs") if e.features.get("A3")]


@pytest.mark.parametrize(("n", "radius"), [(60, 60.0), (300, 25.0)])
@pytest.mark.parametrize("seed", range(12))
def test_dense_uniform_traffic_is_not_a_ring(n: int, radius: float, seed: int) -> None:
    assert not _rings(_uniform_disc(n, radius, seed))


def test_ring_alone_still_fires() -> None:
    assert len(_rings(V.ring_spoof())) == 1


@pytest.mark.parametrize("seed", range(4))
def test_ring_with_dense_background_still_fires(seed: int) -> None:
    ring = V.ring_spoof()
    bg = _uniform_disc(40, 40.0, seed)
    assert len(_rings(ring + bg)) == 1


# ---- gaps once per tick, one AOI each ----
def test_overlapping_aois_claim_a_gap_once() -> None:
    obs = V.dark_gap_covered()
    far_lat, far_lon = destination(V.LAT0, V.LON0, 90.0, 30.0)
    a = AOI(name="a", lat=V.LAT0, lon=V.LON0, radius_nm=100)  # nearer centre
    b = AOI(name="b", lat=far_lat, lon=far_lon, radius_nm=100)  # also contains the last fix
    assert 100 * KM_PER_NM > 30.0
    dets: dict[str, dict[str, Any]] = {"a": {}, "b": {}}
    got = _assign_gaps([a, b], dets, obs, V.GAP_NOW)
    fired = {k: [g for g in v if g["fire"]] for k, v in got.items()}
    assert len(fired["a"]) == 1 and not fired["b"]
    total = sum(len(ais_gap.evidence_from_gaps(v, {}, V.GAP_NOW, aoi=k)) for k, v in got.items())
    assert total == 1


# ---- trailing partial bin ----
@pytest.mark.parametrize(("silent_h", "want_bins"), [(2.2, 2), (2.7, 3)])
def test_short_trailing_bin_is_ignored_but_long_one_counts(silent_h: float, want_bins: int) -> None:
    tgt = V.gap_target(silent_h=silent_h)
    last = V.GAP_NOW - timedelta(hours=silent_h)
    # neighbours heard until 2 h after the last fix, then silent
    bg = V.background_traffic(V.LAT0, V.LON0, end=last + timedelta(hours=2))
    g = [x for x in ais_gap.find_gaps(tgt + bg, {}, V.GAP_NOW) if x["mmsi"] == 111000001]
    assert len(g) == 1
    assert g[0]["covered_frac"] == pytest.approx(2 / want_bins)
