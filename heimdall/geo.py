"""Pure geo/kinematics helpers shared by detectors. No I/O."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

import h3

EARTH_RADIUS_KM = 6371.0088
KM_PER_NM = 1.852
KT_PER_KMH = 1 / 1.852
MACH1_KT = 661.47  # ISA sea level; conservative for "impossible" checks


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def implied_speed_kt(
    lat1: float, lon1: float, t1: float, lat2: float, lon2: float, t2: float
) -> float:
    """Ground speed implied by two fixes (t in epoch seconds)."""
    dt_h = (t2 - t1) / 3600.0
    if dt_h <= 0:
        return float("inf")
    return haversine_km(lat1, lon1, lat2, lon2) / dt_h * KT_PER_KMH


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def angle_diff(a: float, b: float) -> float:
    """Signed smallest difference b - a in degrees, in (-180, 180]."""
    d = (b - a + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def circular_variance(angles_deg: Sequence[float]) -> float:
    """1 - mean resultant length. 0 = all same heading, ~1 = uniformly spread."""
    if not angles_deg:
        return 0.0
    s = sum(math.sin(math.radians(a)) for a in angles_deg)
    c = sum(math.cos(math.radians(a)) for a in angles_deg)
    r = math.hypot(s, c) / len(angles_deg)
    return 1.0 - r


def cumulative_turn_deg(headings_deg: Sequence[float]) -> float:
    """Sum of absolute heading changes along a sequence (handles 359->1 wrap)."""
    return sum(abs(angle_diff(a, b)) for a, b in itertools.pairwise(headings_deg))


def path_length_km(points: Sequence[tuple[float, float]]) -> float:
    return sum(haversine_km(a[0], a[1], b[0], b[1]) for a, b in itertools.pairwise(points))


def displacement_ratio(points: Sequence[tuple[float, float]]) -> float:
    """Net displacement / path length. ~1 straight line, ~0 closed orbit."""
    total = path_length_km(points)
    if total <= 0 or len(points) < 2:
        return 1.0
    net = haversine_km(points[0][0], points[0][1], points[-1][0], points[-1][1])
    return net / total


def h3_cell(lat: float, lon: float, res: int) -> str:
    return str(h3.latlng_to_cell(lat, lon, res))


def h3_parent(cell: str, res: int) -> str:
    return str(h3.cell_to_parent(cell, res))


def h3_resolution(cell: str) -> int:
    return int(h3.get_resolution(cell))


def h3_cell_size_km(cell: str) -> float:
    """Approximate cell diameter (km): twice the mean hexagon edge length at the cell's res."""
    return 2.0 * float(h3.average_hexagon_edge_length(h3.get_resolution(cell), unit="km"))


def h3_center(cell: str) -> tuple[float, float]:
    lat, lon = h3.cell_to_latlng(cell)
    return float(lat), float(lon)


def within_radius_nm(lat: float, lon: float, clat: float, clon: float, radius_nm: float) -> bool:
    r_km = radius_nm * KM_PER_NM
    # exact shortcut (055): the great-circle distance is never less than the latitude difference
    # (111.19 km per degree on the 6371 km sphere); most points of a worldwide window fail here
    if abs(lat - clat) * 111.19 > r_km:
        return False
    return haversine_km(lat, lon, clat, clon) <= r_km


def point_in_polygon(lat: float, lon: float, ring: Sequence[tuple[float, float]]) -> bool:
    """Ray-casting test on (lat, lon) vertices; planar, fine for areas of a few degrees."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def bbox_for_radius(
    lat: float, lon: float, radius_nm: float
) -> list[tuple[float, float, float, float]]:
    """Bounding box(es) (south, west, north, east) covering a circle; antimeridian-safe.

    One box normally, two when the box crosses +-180 (split there), and a full-longitude band
    if the circle contains a pole.
    """
    r_lat = radius_nm * KM_PER_NM / (math.pi * EARTH_RADIUS_KM / 180.0)
    south, north = max(-90.0, lat - r_lat), min(90.0, lat + r_lat)
    if lat + r_lat >= 90.0 or lat - r_lat <= -90.0:
        return [(south, -180.0, north, 180.0)]
    r_lon = r_lat / max(math.cos(math.radians(max(abs(south), abs(north)))), 1e-6)
    if r_lon >= 180.0:
        return [(south, -180.0, north, 180.0)]
    west, east = lon - r_lon, lon + r_lon
    if west < -180.0:
        return [(south, west + 360.0, north, 180.0), (south, -180.0, north, east)]
    if east > 180.0:
        return [(south, west, north, 180.0), (south, -180.0, north, east - 360.0)]
    return [(south, west, north, east)]


Ident = str | int | frozenset[str] | frozenset[int]  # one id, or a set of ids
BBox = tuple[float, float, float, float]  # w, s, e, n (w > e crosses the antimeridian)


def in_bbox(lat: float, lon: float, bb: BBox) -> bool:
    w, s, e, n = bb
    if not s <= lat <= n:
        return False
    return w <= lon <= e if w <= e else (lon >= w or lon <= e)


def obs_match(o: object, bbox: BBox | None, ident: Ident | None, key: str) -> bool:
    """In-memory equivalent of the Store bbox/ident filter (`key` = 'hex' or 'mmsi')."""
    if ident is not None:
        v = getattr(o, key)
        if v not in ident if isinstance(ident, frozenset) else v != ident:
            return False
    return bbox is None or in_bbox(o.lat, o.lon, bbox)  # type: ignore[attr-defined]


def h3_disk(cell: str, k: int) -> list[str]:
    """All cells within `k` grid rings of `cell` (including it)."""
    return [str(c) for c in h3.grid_disk(cell, k)]
