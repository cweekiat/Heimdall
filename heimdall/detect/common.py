"""Shared pure helpers for detectors: ids, robust statistics, window alignment, cell keys.

No I/O. Everything here is deterministic given its inputs.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from heimdall.geo import EARTH_RADIUS_KM, h3_cell
from heimdall.schemas import AircraftObs, VesselObs

#: baseline(key, metric) -> historical values (oldest first). Supplied by the pipeline.
Baseline = Callable[[str, str], list[float]]

MAD_SCALE = 1.4826  # MAD -> sigma for a normal distribution


def evidence_id(
    kind: str, cell: str, entity: str, window_start: datetime, aoi: str | None = None
) -> str:
    """Deterministic id: sha1(kind|aoi|cell|entity|window_start ISO)."""
    raw = f"{kind}|{aoi or ''}|{cell}|{entity}|{window_start.isoformat()}"
    return hashlib.sha1(raw.encode()).hexdigest()


def align_window(now: datetime, window_min: float, step_min: float) -> tuple[datetime, datetime]:
    """Floor `now` to the step grid (aligned to UTC midnight) and return [start, end).

    Assumes `step_min` divides 24 h (5, 10, 15, 30 ... min). Aligning makes ids deterministic
    across re-runs and backtests.
    """
    step_s = max(1, round(step_min * 60))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sod = now.hour * 3600 + now.minute * 60 + now.second
    end = midnight + timedelta(seconds=(sod // step_s) * step_s)
    return end - timedelta(minutes=window_min), end


def in_window(obs: Iterable[AircraftObs], start: datetime, end: datetime) -> list[AircraftObs]:
    return [o for o in obs if start <= o.ts < end]


def cell_of(o: AircraftObs, res: int) -> str:
    """H3 cell of an observation at `res`, reusing precomputed keys when available."""
    if res == 4 and o.h3_r4:
        return o.h3_r4
    if res == 6 and o.h3_r6:
        return o.h3_r6
    return h3_cell(o.lat, o.lon, res)


def median(xs: Sequence[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def mad(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    m = median(xs)
    return median([abs(x - m) for x in xs])


def robust_z(value: float, history: Sequence[float], eps: float) -> tuple[float, float, float]:
    """(z, median, scale) with z = (value - median) / (1.4826*MAD + eps)."""
    med = median(history)
    scale = MAD_SCALE * mad(history) + eps
    return (value - med) / scale, med, scale


def cfg_get(cfg: dict[str, Any], key: str, default: Any) -> Any:
    v = cfg.get(key, default)
    return default if v is None else v


def local_xy_km(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Equirectangular projection around (lat0, lon0), km. Adequate for < ~300 km spans."""
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_RADIUS_KM
    y = math.radians(lat - lat0) * EARTH_RADIUS_KM
    return x, y


def xy_to_latlon(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat = lat0 + math.degrees(y / EARTH_RADIUS_KM)
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_KM * math.cos(math.radians(lat0))))
    return lat, lon


def ellipse_2sigma(
    points: Sequence[tuple[float, float]], floor_km: float = 5.0, n_vertices: int = 36
) -> tuple[float, float, float, float, float, dict[str, Any]]:
    """Centroid + 2-sigma covariance ellipse of (lat, lon) points.

    Returns (lat_c, lon_c, semi_major_km, semi_minor_km, angle_deg_from_east, geojson_polygon).
    A variance floor of floor_km^2 on each axis keeps the ellipse non-degenerate for < 3 points.
    """
    n = len(points)
    lat_c = sum(p[0] for p in points) / n
    lon_c = sum(p[1] for p in points) / n
    xy = [local_xy_km(p[0], p[1], lat_c, lon_c) for p in points]
    sxx = sum(x * x for x, _ in xy) / n + floor_km**2
    syy = sum(y * y for _, y in xy) / n + floor_km**2
    sxy = sum(x * y for x, y in xy) / n
    tr, det = sxx + syy, sxx * syy - sxy * sxy
    disc = math.sqrt(max(0.0, tr * tr / 4 - det))
    l1, l2 = tr / 2 + disc, max(tr / 2 - disc, 0.0)
    angle = 0.5 * math.atan2(2 * sxy, sxx - syy)
    a, b = 2 * math.sqrt(l1), 2 * math.sqrt(l2)
    ring: list[list[float]] = []
    for i in range(n_vertices + 1):
        t = 2 * math.pi * (i % n_vertices) / n_vertices
        ex, ey = a * math.cos(t), b * math.sin(t)
        x = ex * math.cos(angle) - ey * math.sin(angle)
        y = ex * math.sin(angle) + ey * math.cos(angle)
        la, lo = xy_to_latlon(x, y, lat_c, lon_c)
        ring.append([round(lo, 5), round(la, 5)])
    geom = {"type": "Polygon", "coordinates": [ring]}
    return lat_c, lon_c, a, b, math.degrees(angle), geom


def record_id(o: AircraftObs) -> str:
    return f"{o.hex}@{o.ts.isoformat()}"


# --------------------------------------------------------------------------- vessels (AIS)


def valid_vessel_fix(v: VesselObs) -> bool:
    """False for out-of-range coordinates and the (0, 0) "null island" default."""
    if not (-90.0 <= v.lat <= 90.0 and -180.0 <= v.lon <= 180.0):
        return False
    return not (v.lat == 0.0 and v.lon == 0.0)


def vessel_in_window(obs: Iterable[VesselObs], start: datetime, end: datetime) -> list[VesselObs]:
    """Valid fixes with start <= ts < end (invalid positions are dropped here)."""
    return [o for o in obs if start <= o.ts < end and valid_vessel_fix(o)]


def vessel_cell_of(o: VesselObs, res: int) -> str:
    if res == 4 and o.h3_r4:
        return o.h3_r4
    if res == 6 and o.h3_r6:
        return o.h3_r6
    return h3_cell(o.lat, o.lon, res)


def vessel_record_id(o: VesselObs) -> str:
    return f"{o.mmsi}@{o.ts.isoformat()}"


def dedupe_vessel_fixes(fixes: Iterable[VesselObs]) -> list[VesselObs]:
    """One fix per (mmsi, ts) (several receivers hear the same message); ties by provider."""
    out: dict[tuple[int, datetime], VesselObs] = {}
    for o in sorted(fixes, key=lambda o: (o.mmsi, o.ts, o.provider)):
        out.setdefault((o.mmsi, o.ts), o)
    return sorted(out.values(), key=lambda o: (o.mmsi, o.ts))
