"""Pure satellite pass prediction from CelesTrak OMM elements ( s7: computation only).

TEME -> ECEF uses GMST only (no polar motion / nutation): error is far below the ~1 degree
elevation and 60 s timing that "is there a pass over this AOI" needs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sgp4.api import Satrec, jday
from sgp4.omm import initialize
from sgp4.propagation import gstime

from heimdall.geo import h3_cell
from heimdall.schemas import Domain, GeoEvent

STEP_S = 10.0
WGS84_A = 6378.137  # km
WGS84_F = 1 / 298.257223563
OMM_REQUIRED = (
    "EPOCH",
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
    "NORAD_CAT_ID",
    "BSTAR",
    "MEAN_MOTION_DOT",
    "MEAN_MOTION_DDOT",
)
_OMM_DEFAULTS: dict[str, Any] = {
    "CLASSIFICATION_TYPE": "U",
    "OBJECT_ID": "0000-000A",
    "EPHEMERIS_TYPE": 0,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 0,
}


def parse_epoch(v: Any) -> datetime:
    if not isinstance(v, str):
        raise ValueError("EPOCH missing")
    d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def satrec_from_omm(omm: Mapping[str, Any]) -> Satrec:
    """Build a Satrec from an OMM dict; raises ValueError on missing/invalid fields."""
    missing = [k for k in OMM_REQUIRED if omm.get(k) in (None, "")]
    if missing:
        raise ValueError(f"OMM missing {missing}")
    fields = {**_OMM_DEFAULTS, **{k: v for k, v in omm.items() if v is not None}}
    fields["EPOCH"] = parse_epoch(fields["EPOCH"]).strftime("%Y-%m-%dT%H:%M:%S.%f")
    sat = Satrec()
    try:
        initialize(sat, fields)
    except (KeyError, TypeError) as e:
        raise ValueError(f"bad OMM: {e}") from e
    return sat


def _observer_ecef(lat: float, lon: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Observer ECEF position (km) and local east/north/up unit vectors."""
    la, lo = math.radians(lat), math.radians(lon)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A / math.sqrt(1 - e2 * math.sin(la) ** 2)
    pos = np.array(
        [
            n * math.cos(la) * math.cos(lo),
            n * math.cos(la) * math.sin(lo),
            n * (1 - e2) * math.sin(la),
        ]
    )
    east = np.array([-math.sin(lo), math.cos(lo), 0.0])
    north = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    return pos, np.stack([east, north, up]), up


def elevations(
    sat: Satrec, lat: float, lon: float, start: datetime, n: int, step_s: float = STEP_S
) -> np.ndarray:
    """Elevation angle (deg, NaN where SGP4 fails) at n samples from `start`."""
    t0 = start.astimezone(UTC)
    offs = np.arange(n) * step_s
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second + t0.microsecond / 1e6)
    fr = fr0 + offs / 86400.0
    jd = np.full(n, jd0)
    err, r, _v = sat.sgp4_array(jd, fr)
    theta = np.array([gstime(a + b) for a, b in zip(jd, fr, strict=True)])
    c, s = np.cos(theta), np.sin(theta)
    x = c * r[:, 0] + s * r[:, 1]  # TEME -> ECEF (rotate by GMST about z)
    y = -s * r[:, 0] + c * r[:, 1]
    ecef = np.stack([x, y, r[:, 2]], axis=1)
    obs, _enu, up = _observer_ecef(lat, lon)
    rho = ecef - obs
    rng = np.linalg.norm(rho, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        el = np.degrees(np.arcsin((rho @ up) / rng))
    el[err != 0] = np.nan
    return np.asarray(el)


def passes(
    omm: Mapping[str, Any],
    aoi_center: tuple[float, float],
    start: datetime,
    hours: float,
    min_elev_deg: float,
    *,
    aoi: str = "",
    sensor: str = "",
) -> list[GeoEvent]:
    """Predicted `overpass` events (max elevation >= min_elev_deg) over the AOI centre."""
    sat = satrec_from_omm(omm)
    lat, lon = aoi_center
    n = int(hours * 3600 / STEP_S) + 1
    el = elevations(sat, lat, lon, start, n)
    above = np.nan_to_num(el, nan=-90.0) >= min_elev_deg
    out: list[GeoEvent] = []
    t0 = start.astimezone(UTC)
    i = 0
    while i < n:
        if not above[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and above[j + 1]:
            j += 1
        k = i + int(np.argmax(el[i : j + 1]))
        rise, peak, sett = (t0 + timedelta(seconds=x * STEP_S) for x in (i, k, j))
        norad = int(omm["NORAD_CAT_ID"])
        name = str(omm.get("OBJECT_NAME") or norad)[:40]
        max_el = float(el[k])
        out.append(
            GeoEvent(
                id=f"celestrak|overpass|{norad}|{aoi}|{rise.strftime('%Y%m%dT%H%M%S')}",
                ts=peak,
                source="celestrak",
                domain=Domain.SPACE,
                kind="overpass",
                lat=lat,
                lon=lon,
                valid_from=rise,
                valid_to=sett,
                title=f"{name} overpass",
                value=max_el,
                attrs={
                    "role": "context",
                    "norad_id": norad,
                    "sat_name": name,
                    "sensor": sensor,
                    "aoi": aoi,
                    "max_elev_deg": max_el,
                },
                h3_r4=h3_cell(lat, lon, 4),
                h3_r6=h3_cell(lat, lon, 6),
            )
        )
        i = j + 1
    return out


def ground_track(
    sat: Satrec, start: datetime, n: int, step_s: float
) -> list[tuple[float, float, float] | None]:
    """(lat, lon, alt_km) sub-satellite points at n samples from `start` (None where SGP4 fails).

    TEME -> ECEF by GMST rotation (as `elevations`), then ECEF -> geodetic (WGS84, Bowring)."""
    t0 = start.astimezone(UTC)
    offs = np.arange(n) * step_s
    jd0, fr0 = jday(t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second + t0.microsecond / 1e6)
    fr = fr0 + offs / 86400.0
    jd = np.full(n, jd0)
    err, r, _v = sat.sgp4_array(jd, fr)
    theta = np.array([gstime(a + b) for a, b in zip(jd, fr, strict=True)])
    c, s = np.cos(theta), np.sin(theta)
    x = c * r[:, 0] + s * r[:, 1]
    y = -s * r[:, 0] + c * r[:, 1]
    z = r[:, 2]
    e2 = WGS84_F * (2 - WGS84_F)
    b = WGS84_A * (1 - WGS84_F)
    ep2 = (WGS84_A**2 - b**2) / b**2
    p = np.hypot(x, y)
    th = np.arctan2(z * WGS84_A, p * b)
    lat = np.arctan2(z + ep2 * b * np.sin(th) ** 3, p - e2 * WGS84_A * np.cos(th) ** 3)
    lon = np.arctan2(y, x)
    nrad = WGS84_A / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    with np.errstate(invalid="ignore", divide="ignore"):
        alt = p / np.cos(lat) - nrad
    out: list[tuple[float, float, float] | None] = []
    for i in range(n):
        if err[i] != 0 or not np.isfinite(lat[i]) or not np.isfinite(alt[i]):
            out.append(None)
            continue
        out.append(
            (
                round(float(np.degrees(lat[i])), 4),
                round(float(np.degrees(lon[i])), 4),
                round(float(alt[i]), 1),
            )
        )
    return out
