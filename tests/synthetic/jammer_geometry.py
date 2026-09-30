"""Synthetic ground jammer with radio-horizon coverage (task 029a).

Aircraft at mixed altitudes are scattered around a known emitter. A fix is degraded (NIC/NACp 0)
exactly when it is inside 4.12 (sqrt(h) + sqrt(10)) km of the emitter.
"""

from __future__ import annotations

import math
import random
from datetime import timedelta

from heimdall.schemas import AircraftObs
from tests.synthetic.generators import T0, destination, make_obs


def horizon_km(alt_m: float, hj: float = 10.0) -> float:
    return 4.12 * (math.sqrt(alt_m) + math.sqrt(hj))


def _flat_km(la: float, lo: float, la0: float, lo0: float) -> float:
    x = math.radians(lo - lo0) * math.cos(math.radians(la0)) * 6371.0
    y = math.radians(la - la0) * 6371.0
    return math.hypot(x, y)


def jammer_scene(
    jam_lat: float = 20.0,
    jam_lon: float = 96.0,
    n_aircraft: int = 80,
    seed: int = 3,
    alt_range_m: tuple[float, float] = (600.0, 11000.0),
    max_km: float = 380.0,
    with_alt: bool = True,
) -> list[AircraftObs]:
    """Aircraft on straight tracks; each fix is degraded iff inside its own horizon range."""
    rng = random.Random(seed)
    out: list[AircraftObs] = []
    for k in range(n_aircraft):
        alt_m = rng.uniform(*alt_range_m)
        la0, lo0 = destination(
            jam_lat, jam_lon, rng.uniform(0, 360), max_km * math.sqrt(rng.random())
        )
        hdg = rng.uniform(0, 360)
        for i in range(12):
            t = T0 + timedelta(minutes=2 + i * 2)
            la, lo = destination(la0, lo0, hdg, 15.0 * i)
            bad = _flat_km(la, lo, jam_lat, jam_lon) <= horizon_km(alt_m)
            kw: dict[str, int | None] = {"nic": 0, "nac_p": 0} if bad else {}
            ft = int(alt_m / 0.3048) if with_alt else None
            kw.update(alt_geom=ft, alt_baro=ft)
            out.append(make_obs(f"d{k:05x}", t, la, lo, track=hdg, **kw))
    return out
