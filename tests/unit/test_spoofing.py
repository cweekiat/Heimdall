from __future__ import annotations

from datetime import timedelta

from heimdall.detect import spoofing
from tests.synthetic.generators import (
    T0,
    alt_divergence,
    make_obs,
    mlat_vs_adsb,
    single_glitch,
    spoof_cluster,
    straight_flight,
    teleport_spoof,
)

NOW = T0 + timedelta(minutes=30)


def _fired(obs: list, hex_: str) -> set[str]:
    s = spoofing.aircraft_signals(obs, {}, NOW)[hex_]
    return {k for k, v in s.fired.items() if v}


def test_teleport_with_healthy_nic_alerts() -> None:
    obs = teleport_spoof()
    assert _fired(obs, "c00001") == {"S1", "S5"}
    (ev,) = spoofing.detect(obs, {}, NOW)
    assert ev.kind == "gnss_spoofing" and ev.entities == ["c00001"]
    assert ev.detector_version == spoofing.VERSION
    assert ev.features["jump_km"] > 100
    assert ev.geometry and ev.geometry["type"] == "LineString"
    assert ev.records and ev.counterfactual and "glitch" in ev.counterfactual
    # evidence is localised at the last good fix, not the spoofed destination
    assert abs(ev.lat - 20.0) < 1.0  # type: ignore[operator]


def test_teleport_with_degraded_nic_is_single_signal_no_alert() -> None:
    """Bad avionics / jamming: position jumps but NIC is low -> S1 only -> no alert."""
    obs = teleport_spoof(nic=3)
    assert _fired(obs, "c00001") == {"S1"}
    assert spoofing.detect(obs, {}, NOW) == []


def test_single_glitch_is_removed() -> None:
    obs = single_glitch()
    s = spoofing.aircraft_signals(obs, {}, NOW)["c00002"]
    assert s.features["n_glitches"] == 1 and not s.fired["S1"]
    assert spoofing.detect(obs, {}, NOW) == []


def test_straight_flight_clean() -> None:
    assert _fired(straight_flight(minutes=30), "a00001") == set()


def _neighbours(lon: float = 96.0) -> list:
    """Three healthy aircraft in the same cell: the regional per-foot norm S2 needs."""
    return [
        o
        for k in range(3)
        for o in alt_divergence(f"c2000{k}", offset_ft=0, lon=lon + 0.01 * (k + 1))
    ]


def test_alt_divergence_needs_a_regional_norm() -> None:
    """041: alone in its cell an aircraft gets no altitude verdict (no norm, no S2)."""
    assert _fired(alt_divergence(), "c00003") == set()


def test_alt_divergence_alone_is_one_signal() -> None:
    obs = alt_divergence() + _neighbours()
    assert _fired(obs, "c00003") == {"S2"}
    assert spoofing.detect(obs, {}, NOW) == []


def test_alt_divergence_relative_to_regional_norm() -> None:
    """Everyone in the cell shows +2500 ft geom-baro (hot day): norm absorbs it, S2 silent."""
    obs = []
    for k in range(4):
        obs += alt_divergence(hex_=f"c1000{k}", lon=96.0 + k * 0.01)
    assert all(not s.fired["S2"] for s in spoofing.aircraft_signals(obs, {}, NOW).values())


def test_alt_divergence_plus_teleport_alerts() -> None:
    obs = [
        o.model_copy(update={"alt_geom": (o.alt_baro or 0) + 2500}) for o in teleport_spoof(nic=3)
    ]
    obs += _neighbours(lon=obs[0].lon)
    (ev,) = [e for e in spoofing.detect(obs, {}, NOW) if not e.entities[0].startswith("c2000")]
    assert ev.features["S1"] == 1 and ev.features["S2"] == 1


def test_mlat_gap_alone_alerts() -> None:
    obs = mlat_vs_adsb(offset_km=20.0)
    assert "S3" in _fired(obs, "c00004")
    (ev,) = spoofing.detect(obs, {}, NOW)
    assert ev.features["mlat_bad_pairs"] >= 2


def test_mlat_small_gap_no_alert() -> None:
    obs = mlat_vs_adsb(offset_km=0.5)
    assert _fired(obs, "c00004") == set()
    assert spoofing.detect(obs, {}, NOW) == []


def test_mlat_only_aircraft_no_spoof_evidence() -> None:
    obs = [
        o.model_copy(update={"src_type": "mlat", "nic": None, "nac_p": None})
        for o in teleport_spoof()
    ]
    assert spoofing.detect(obs, {}, NOW) == []


def test_point_spoof_cluster_alerts_every_member() -> None:
    evs = spoofing.detect(spoof_cluster(n=4), {}, NOW)
    assert len(evs) == 4
    assert all(e.features["S4"] == 1 for e in evs)
    assert len({e.h3_cell for e in evs}) == 1


def test_formation_same_track_is_not_s4() -> None:
    obs = []
    for k in range(3):
        obs += straight_flight(hex_=f"f0000{k}", lat=20.0 + k * 0.005, heading=90.0, minutes=20)
    sigs = spoofing.aircraft_signals(obs, {}, NOW)
    assert all(not s.fired["S4"] for s in sigs.values())


def test_min_dt_thinning_ignores_duplicate_provider_fixes() -> None:
    base = straight_flight(minutes=20)
    dup = [
        make_obs(o.hex, o.ts + timedelta(seconds=1), o.lat + 0.001, o.lon, provider="adsb.fi")
        for o in base
    ]
    assert _fired(base + dup, "a00001") == set()


def test_ids_deterministic() -> None:
    a = spoofing.detect(teleport_spoof(), {}, NOW)[0]
    b = spoofing.detect(teleport_spoof(), {}, NOW + timedelta(minutes=3))[0]
    assert a.id == b.id


def test_timestamp_jitter_on_a_normal_airliner_is_not_spoofing() -> None:
    """Sparse-feeder aggregators stamp fixes up to a few seconds late (Malacca capture 2026-09-30).
    A straight 470 kt flight with +/-5 s timestamp error must not trip S1, and healthy NIC (S5)
    must not stand in for a second signal."""
    clean = straight_flight("a0j001", speed_kt=470.0, dt_s=7.0)
    jitter = [5.0, -5.0, 4.0, -4.0, 0.0, 5.0, -3.0]
    obs = [
        o.model_copy(update={"ts": o.ts + timedelta(seconds=jitter[i % 7])})
        for i, o in enumerate(clean)
    ]
    assert "S1" not in _fired(obs, "a0j001")
    assert spoofing.detect(obs, {}, NOW) == []


def test_speed_mismatch_with_healthy_nic_alone_does_not_alert() -> None:
    """Old behaviour (no jitter tolerance, S5 always counts) alerted on this jittered airliner."""
    clean = straight_flight("a0j002", speed_kt=470.0, dt_s=7.0)
    jitter = [5.0, -5.0, 4.0, -4.0, 0.0, 5.0, -3.0]
    obs = [
        o.model_copy(update={"ts": o.ts + timedelta(seconds=jitter[i % 7])})
        for i, o in enumerate(clean)
    ]
    old = {
        "timing_jitter_s": 0.0,
        "s5_counts_as_signal": True,
        "min_error_km": 0.0,
        "min_displaced_s": 0,
    }
    assert spoofing.detect(obs, old, NOW)  # the false positive we fixed
    assert spoofing.detect(obs, {**old, "s5_counts_as_signal": False}, NOW) == []  # S5 rule
