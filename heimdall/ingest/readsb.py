"""Pure parser: readsb v2 JSON (adsb.lol / adsb.fi / airplanes.live) -> AircraftObs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from heimdall.geo import h3_cell
from heimdall.schemas import AircraftObs

MAX_ALT_FT = 100_000
MIN_ALT_FT = -2_000
MAX_GS_KT = 2_000.0  # nothing civil or military at ADS-B altitudes is faster
MAX_SEEN_POS_S = 3_600.0


def _int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _plausible(lat: float, lon: float, alt: int | None, gs: float | None, seen: float) -> bool:
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    if lat == 0.0 and lon == 0.0:  # null island
        return False
    if alt is not None and not (MIN_ALT_FT <= alt <= MAX_ALT_FT):
        return False
    if gs is not None and not (0.0 <= gs <= MAX_GS_KT):
        return False
    return 0.0 <= seen <= MAX_SEEN_POS_S


def parse_readsb_v2(
    payload: dict[str, Any], provider: str, fetched_at: datetime
) -> tuple[list[AircraftObs], int]:
    """Return (observations, n_dropped_implausible). Rows with no position are skipped."""
    out: list[AircraftObs] = []
    dropped = 0
    for ac in payload.get("ac") or payload.get("aircraft") or []:
        if not isinstance(ac, dict):
            dropped += 1
            continue
        lat, lon = _float(ac.get("lat")), _float(ac.get("lon"))
        hex_ = str(ac.get("hex") or "").strip().lower()
        if lat is None or lon is None:
            continue  # no position (e.g. Mode S only): not a plausibility failure
        if not hex_:
            dropped += 1
            continue
        raw_alt = ac.get("alt_baro")
        on_ground = raw_alt == "ground"
        alt_baro = None if on_ground else _int(raw_alt)
        gs = _float(ac.get("gs"))
        seen_pos = _float(ac.get("seen_pos"))
        if not _plausible(lat, lon, alt_baro, gs, seen_pos or 0.0):
            dropped += 1
            continue
        db_flags = _int(ac.get("dbFlags")) or 0
        flight = ac.get("flight")
        out.append(
            AircraftObs(
                ts=fetched_at - timedelta(seconds=seen_pos or 0.0),
                hex=hex_,
                provider=provider,
                lat=lat,
                lon=lon,
                src_type=str(ac.get("type") or "other"),
                flight=flight.strip() or None if isinstance(flight, str) else None,
                type_code=ac.get("t") or None,
                registration=ac.get("r") or None,
                mil=bool(db_flags & 1),
                alt_baro=alt_baro,
                alt_geom=_int(ac.get("alt_geom")),
                on_ground=on_ground,
                gs=gs,
                track=_float(ac.get("track")),
                baro_rate=_int(ac.get("baro_rate")),
                geom_rate=_int(ac.get("geom_rate")),
                nic=_int(ac.get("nic")),
                nac_p=_int(ac.get("nac_p")),
                sil=_int(ac.get("sil")),
                rc=_int(ac.get("rc")),
                gps_ok_before=_float(ac.get("gpsOkBefore")),
                seen_pos=seen_pos,
                h3_r4=h3_cell(lat, lon, 4),
                h3_r6=h3_cell(lat, lon, 6),
            )
        )
    return out, dropped
