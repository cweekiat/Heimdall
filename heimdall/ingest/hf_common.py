"""Shared helpers for HF propagation sources (WSPR, PSK Reporter),.

HF signals reflect off the ionosphere, so a path's anomaly is mapped at the great-circle midpoint
of transmitter and receiver (the reflection region), not at either station. Spots are aggregated
per (time bin, amateur band, midpoint grid cell) into `hf_spots` GeoEvents.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta

# Amateur HF/MF bands (MHz label -> Hz range). WSPR Live's `band` column uses the same labels.
HF_BANDS: dict[int, tuple[float, float]] = {
    1: (1.8e6, 2.0e6),
    3: (3.5e6, 4.0e6),
    5: (5.25e6, 5.45e6),
    7: (7.0e6, 7.3e6),
    10: (10.1e6, 10.15e6),
    14: (14.0e6, 14.35e6),
    18: (18.068e6, 18.168e6),
    21: (21.0e6, 21.45e6),
    24: (24.89e6, 24.99e6),
    28: (28.0e6, 29.7e6),
}

_LOC = re.compile(r"^[A-Ra-r]{2}(\d{2}([A-Xa-x]{2}(\d{2})?)?)?$")


def band_of(freq_hz: float) -> int | None:
    """Amateur band label (MHz) for a frequency in Hz, None outside 1.8-29.7 MHz."""
    for label, (lo, hi) in HF_BANDS.items():
        if lo <= freq_hz <= hi:
            return label
    return None


def maidenhead(loc: str | None) -> tuple[float, float] | None:
    """Centre (lat, lon) of a 2/4/6/8-character Maidenhead locator, None if malformed."""
    if not isinstance(loc, str) or not _LOC.match(loc.strip()):
        return None
    s = loc.strip()
    lon, lat = -180.0, -90.0
    lon += (ord(s[0].upper()) - 65) * 20
    lat += (ord(s[1].upper()) - 65) * 10
    w, h = 20.0, 10.0
    if len(s) >= 4:
        lon += int(s[2]) * 2
        lat += int(s[3])
        w, h = 2.0, 1.0
    if len(s) >= 6:
        lon += (ord(s[4].lower()) - 97) * (5 / 60)
        lat += (ord(s[5].lower()) - 97) * (2.5 / 60)
        w, h = 5 / 60, 2.5 / 60
    if len(s) >= 8:
        lon += int(s[6]) * (0.5 / 60)
        lat += int(s[7]) * (0.25 / 60)
        w, h = 0.5 / 60, 0.25 / 60
    return lat + h / 2, lon + w / 2


def midpoint(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Great-circle midpoint (lat, lon) in degrees; lon normalised to [-180, 180)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    bx, by = math.cos(p2) * math.cos(dl), math.cos(p2) * math.sin(dl)
    lat = math.atan2(math.sin(p1) + math.sin(p2), math.hypot(math.cos(p1) + bx, by))
    lon = math.radians(lon1) + math.atan2(by, math.cos(p1) + bx)
    return math.degrees(lat), (math.degrees(lon) + 540.0) % 360.0 - 180.0


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def grid(v: float, deg: float) -> float:
    """Snap to the centre-aligned grid used by the WSPR SQL (round(v / deg) * deg)."""
    return round(v / deg) * deg


def bin_start(ts: datetime, minutes: int) -> datetime:
    ts = ts.astimezone(UTC)
    return ts.replace(minute=ts.minute - ts.minute % minutes, second=0, microsecond=0)


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    """bbox = (west, south, east, north); no antimeridian crossing."""
    w, s, e, n = bbox
    return s <= lat <= n and w <= lon <= e


def spot_id(source: str, t: datetime, band: int, glat: float, glon: float) -> str:
    return f"{source}|{t.strftime('%Y%m%dT%H%M')}|{band}|{glat:g}|{glon:g}"


def bin_window(t: datetime, minutes: int) -> tuple[datetime, datetime]:
    return t, t + timedelta(minutes=minutes)
