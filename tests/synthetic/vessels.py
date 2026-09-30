"""Synthetic AIS generators for the AIS spoofing / dark-gap detector tests. Deterministic, offline.

All generators return `list[VesselObs]` with h3 keys filled like the ingest layer does.
Spoofing scenarios lie in [T0, T0 + 60 min) (detect at NOW = T0 + 60 min); gap scenarios span the
24 h before GAP_NOW.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from heimdall.geo import h3_cell
from heimdall.schemas import VesselObs
from tests.synthetic.generators import T0, destination

NOW = T0 + timedelta(minutes=60)
GAP_NOW = T0 + timedelta(hours=24)
LAT0, LON0 = 5.0, 105.0  # open South China Sea water


def make_vessel(mmsi: int, ts: datetime, lat: float, lon: float, **kw: Any) -> VesselObs:
    base: dict[str, Any] = {"provider": "aisstream", "sog": 10.0, "cog": 90.0, "nav_status": 0}
    base.update(kw)
    return VesselObs(
        ts=ts,
        mmsi=mmsi,
        lat=lat,
        lon=lon,
        h3_r4=h3_cell(lat, lon, 4),
        h3_r6=h3_cell(lat, lon, 6),
        **base,
    )


def straight_transit(
    mmsi: int = 200000001,
    lat: float = LAT0,
    lon: float = LON0,
    bearing: float = 90.0,
    speed_kn: float = 12.0,
    n: int = 30,
    dt_s: int = 120,
    t0: datetime = T0,
) -> list[VesselObs]:
    out = []
    for i in range(n):
        la, lo = destination(lat, lon, bearing, speed_kn * 1.852 * i * dt_s / 3600.0)
        out.append(make_vessel(mmsi, t0 + timedelta(seconds=i * dt_s), la, lo, sog=speed_kn))
    return out


def anchored_in_port(
    mmsi: int = 200000002, lat: float = LAT0, lon: float = LON0, n: int = 30, dt_s: int = 120
) -> list[VesselObs]:
    rng = random.Random(mmsi)
    return [
        make_vessel(
            mmsi,
            T0 + timedelta(seconds=i * dt_s),
            lat + rng.uniform(-2e-5, 2e-5),
            lon + rng.uniform(-2e-5, 2e-5),
            sog=0.0,
            nav_status=1,
        )
        for i in range(n)
    ]


def ring_spoof(
    n_vessels: int = 8,
    radius_km: float = 5.0,
    lat: float = LAT0,
    lon: float = LON0,
    n_bins: int = 4,
    base_mmsi: int = 300000000,
) -> list[VesselObs]:
    """Static ring of MMSIs at a fixed radius, evenly spaced bearings, reporting for n_bins bins."""
    out = []
    for b in range(n_bins):
        for k in range(n_vessels):
            la, lo = destination(lat, lon, 360.0 * k / n_vessels, radius_km)
            ts = T0 + timedelta(seconds=b * 300 + 30 + k)
            out.append(make_vessel(base_mmsi + k, ts, la, lo, sog=0.0))
    return out


def circle_track(
    mmsi: int = 400000001,
    laps: float = 3.0,
    radius_km: float = 1.0,
    n: int = 60,
    lat: float = LAT0,
    lon: float = LON0,
    dt_s: int = 60,
    sog: float = 15.0,
) -> list[VesselObs]:
    out = []
    for i in range(n):
        brg = (360.0 * laps * i / n) % 360.0
        la, lo = destination(lat, lon, brg, radius_km)
        out.append(make_vessel(mmsi, T0 + timedelta(seconds=i * dt_s), la, lo, sog=sog))
    return out


def purse_seine_set(mmsi: int = 400000002, **kw: Any) -> list[VesselObs]:
    """One lap of a net set: same geometry as a spoofed circle but only 360 degrees."""
    return circle_track(mmsi, laps=1.0, radius_km=0.3, n=25, sog=3.0, **kw)


def teleport(mmsi: int = 500000001, jump_km: float = 200.0, n: int = 30) -> list[VesselObs]:
    """Transit that jumps `jump_km` at fix n/2 and continues from the new position."""
    out = []
    for i in range(n):
        la, lo = destination(LAT0, LON0, 90.0, 0.37 * i)  # ~12 kn at 120 s
        if i >= n // 2:
            la, lo = destination(la, lo, 0.0, jump_km)
        out.append(make_vessel(mmsi, T0 + timedelta(seconds=i * 120), la, lo))
    return out


def glitch(mmsi: int = 500000002, n: int = 30) -> list[VesselObs]:
    """Transit with one out-and-back outlier fix 300 km away (a single receiver glitch)."""
    out = straight_transit(mmsi, n=n)
    k = n // 2
    o = out[k]
    la, lo = destination(o.lat, o.lon, 0.0, 300.0)
    out[k] = make_vessel(mmsi, o.ts, la, lo)
    return out


def mmsi_collision(mmsi: int = 600000001, sep_km: float = 300.0, n: int = 30) -> list[VesselObs]:
    """Two ships sharing one MMSI, alternating fixes between two places `sep_km` apart."""
    out = []
    for i in range(n):
        base = i // 2
        la, lo = destination(LAT0, LON0, 90.0, 0.37 * base)
        if i % 2:
            la, lo = destination(la, lo, 0.0, sep_km)
        out.append(make_vessel(mmsi, T0 + timedelta(seconds=i * 60), la, lo))
    return out


def identical_positions(
    n_vessels: int = 3, base_mmsi: int = 700000000, n: int = 6
) -> list[VesselObs]:
    """`n_vessels` distinct MMSIs reporting the exact same position, several times."""
    return [
        make_vessel(base_mmsi + k, T0 + timedelta(seconds=60 * i + k), LAT0 + 0.5, LON0, sog=0.0)
        for i in range(n)
        for k in range(n_vessels)
    ]


def rafted_ships(n_vessels: int = 3, base_mmsi: int = 800000000, n: int = 6) -> list[VesselObs]:
    """Ships moored side by side ~20 m apart (must not look like identical positions)."""
    out = []
    for i in range(n):
        for k in range(n_vessels):
            la, lo = destination(LAT0 + 0.5, LON0, 90.0, 0.02 * k)
            out.append(
                make_vessel(
                    base_mmsi + k,
                    T0 + timedelta(seconds=60 * i + k),
                    la,
                    lo,
                    sog=0.0,
                    nav_status=5,
                )
            )
    return out


# --------------------------------------------------------------------------- dark gaps


def background_traffic(
    lat: float,
    lon: float,
    n_ships: int = 6,
    end: datetime = GAP_NOW,
    hours: float = 24.0,
    dt_min: int = 10,
    base_mmsi: int = 900000000,
) -> list[VesselObs]:
    """Ships loitering within ~5 km of (lat, lon), reporting every `dt_min` for `hours`."""
    out = []
    steps = int(hours * 60 / dt_min)
    for k in range(n_ships):
        la, lo = destination(lat, lon, 360.0 * k / n_ships, 3.0)
        for i in range(steps):
            ts = end - timedelta(minutes=dt_min * (steps - i)) + timedelta(seconds=7 * k)
            out.append(make_vessel(base_mmsi + k, ts, la, lo, sog=2.0))
    return out


def gap_target(
    mmsi: int = 111000001,
    silent_h: float = 5.0,
    resume_after_h: float | None = None,
    lat: float = LAT0,
    lon: float = LON0,
    nav_status: int = 0,
    sog: float = 8.0,
    end: datetime = GAP_NOW,
) -> list[VesselObs]:
    """Vessel reporting every 10 min, ending at (lat, lon), silent for `silent_h` before `end`.

    Its last fix is at `end - silent_h`. With `resume_after_h`, it reappears that many hours after
    the last fix (a closed gap).
    """
    last = end - timedelta(hours=silent_h)
    out = []
    for i in range(60):
        ts = last - timedelta(minutes=10 * (59 - i))
        la, lo = destination(lat, lon, 270.0, 0.5 * (59 - i))  # arrives at (lat, lon)
        out.append(make_vessel(mmsi, ts, la, lo, sog=sog, nav_status=nav_status))
    if resume_after_h is not None:
        la, lo = destination(lat, lon, 90.0, 20.0 * resume_after_h)
        out.append(make_vessel(mmsi, last + timedelta(hours=resume_after_h), la, lo, sog=sog))
    return out


def dark_gap_covered() -> list[VesselObs]:
    return gap_target() + background_traffic(LAT0, LON0)


def gap_uncovered() -> list[VesselObs]:
    """Same gap, but the nearest other traffic is ~500 km away (a coverage hole)."""
    la, lo = destination(LAT0, LON0, 0.0, 500.0)
    return gap_target() + background_traffic(la, lo)


def anchored_gap() -> list[VesselObs]:
    return gap_target(nav_status=1, sog=0.0) + background_traffic(LAT0, LON0)


def short_gap() -> list[VesselObs]:
    return gap_target(silent_h=1.5) + background_traffic(LAT0, LON0)
