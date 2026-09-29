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


def h3_center(cell: str) -> tuple[float, float]:
    lat, lon = h3.cell_to_latlng(cell)
    return float(lat), float(lon)


def within_radius_nm(lat: float, lon: float, clat: float, clon: float, radius_nm: float) -> bool:
    return haversine_km(lat, lon, clat, clon) <= radius_nm * KM_PER_NM
