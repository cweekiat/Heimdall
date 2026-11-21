"""Confounder assessment: weather / space weather only LOWER an alert's confidence.

Pure function; never creates Evidence or alerts, never raises severity, never touches Admiralty.

Assumptions
-----------
* `context` may hold any GeoEvents; only `weather` and `space_weather` are read here. Context is
  "matching" when `window_start - max_age_h <= ts <= window_end`. Forecast-API values are hourly,
  Kp is 3-hourly, so `max_age_h` (6) bridges both.
* Rules per `AlertKind` (tiers do not stack within one rule; different rules multiply):
  GNSS_JAMMING: Kp >= kp_severe -> kp_severe_factor, elif Kp >= kp_minor -> kp_minor_factor;
  equatorial scintillation (|lat| <= scint_lat_max and local solar time in `scint_hours`, wrapping
  midnight) -> scint_factor (needs no context event: it is a climatological effect).
  AIS_GAP: max wind_speed_ms >= wind_ms within `storm_km` -> wind_factor.
* Product is clamped to [min_factor, 1]; each single factor is clamped to <= 1 so a bad config
  can never raise confidence. Non-finite or non-numeric attrs are ignored.
* Untrusted text: `text`/`title`/`url` of context events are never read. Notes are templates over
  numeric attributes only.
* Missing matching context -> factor 1 (absence of evidence is not evidence) plus a gap string.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from heimdall.detect.common import cfg_get
from heimdall.geo import haversine_km
from heimdall.schemas import AlertKind, Corroboration, Domain, Evidence, GeoEvent

DEFAULTS: dict[str, Any] = {
    "kp_minor": 5,
    "kp_minor_factor": 0.8,
    "kp_severe": 7,
    "kp_severe_factor": 0.5,
    "scint_lat_max": 20,
    "scint_hours": [19, 2],
    "scint_factor": 0.85,
    "storm_km": 50,
    "wind_ms": 20,
    "wind_factor": 0.9,
    "min_factor": 0.3,
    "max_age_h": 6,
}


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def _num(x: Any) -> float | None:
    """Finite float or None (bool and strings are rejected)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    f = float(x)
    return f if math.isfinite(f) else None


def _f(x: Any) -> float:
    """A multiplicative factor clamped to [0, 1]."""
    v = _num(x)
    return 1.0 if v is None else max(0.0, min(1.0, v))


def local_solar_hour(hour_utc: float, lon: float) -> float:
    return (hour_utc + lon / 15.0) % 24.0


def _in_hours(h: float, start: float, end: float) -> bool:
    return (start <= h < end) if start <= end else (h >= start or h < end)


def assess(
    ev: Evidence, context: Sequence[GeoEvent], cfg: dict[str, Any]
) -> tuple[float, list[Corroboration], list[str]]:
    """(confidence factor in [min_factor, 1], context notes, gap strings) for one Evidence."""
    c = _cfg(cfg)
    if ev.kind not in (AlertKind.GNSS_JAMMING, AlertKind.AIS_GAP):
        return 1.0, [], []
    if ev.detector == "cygnss":  # daily orbit product: Kp/scintillation affect tracking, not the
        return 1.0, [], []  # noise floor; its own space-weather check is in detect/cygnss.py
    t0 = ev.window_start - timedelta(hours=float(c["max_age_h"]))
    t1 = ev.window_end
    fresh = [e for e in context if t0 <= e.ts <= t1]
    notes: list[Corroboration] = []
    gaps: list[str] = []
    factor = 1.0

    def note(domain: Domain, ref: str, text: str, f: float) -> None:
        notes.append(
            Corroboration(
                domain=domain, evidence_id=ref, relation="context", note=text, factor=_f(f)
            )
        )

    def nearby(kind: str) -> list[GeoEvent]:
        if ev.lat is None or ev.lon is None:
            return []
        return [
            e
            for e in fresh
            if e.kind == kind
            and e.lat is not None
            and e.lon is not None
            and haversine_km(ev.lat, ev.lon, e.lat, e.lon) <= float(c["storm_km"])
        ]

    if ev.kind == AlertKind.GNSS_JAMMING:
        sw = [(e, _num(e.attrs.get("kp", e.value))) for e in fresh if e.kind == "space_weather"]
        kps = [(e, k) for e, k in sw if k is not None]
        if not kps:
            gaps.append("space weather context unavailable (lag)")
        else:
            top, kp = max(kps, key=lambda t: t[1])
            fk: float | None = None
            if kp >= float(c["kp_severe"]):
                fk = _f(c["kp_severe_factor"])
            elif kp >= float(c["kp_minor"]):
                fk = _f(c["kp_minor_factor"])
            if fk is not None:
                factor *= fk
                g = _num(top.attrs.get("g_scale"))
                gs = f" (G{int(g)})" if g is not None and g >= 1 else ""
                note(top.domain, top.id, f"Kp {kp:.1f}{gs} during window", fk)
        if (
            ev.lat is not None
            and ev.lon is not None
            and abs(ev.lat) <= float(c["scint_lat_max"])
            and len(c["scint_hours"]) == 2
        ):
            mid = ev.window_start + (ev.window_end - ev.window_start) / 2
            hour = mid.hour + mid.minute / 60.0
            lst = local_solar_hour(hour, ev.lon)
            if _in_hours(lst, float(c["scint_hours"][0]), float(c["scint_hours"][1])):
                factor *= _f(c["scint_factor"])
                note(
                    Domain.SPACE,
                    ev.id,
                    f"Equatorial scintillation window: local solar time {lst:.1f} h "
                    f"at latitude {ev.lat:.1f}",
                    _f(c["scint_factor"]),
                )
    else:
        wx = nearby("weather")
        if not wx:
            gaps.append("weather context unavailable (lag)")
        else:  # AIS_GAP
            winds = [(w, e) for e in wx if (w := _num(e.attrs.get("wind_speed_ms"))) is not None]
            if winds:
                w, e0 = max(winds, key=lambda t: t[0])
                if w >= float(c["wind_ms"]):
                    factor *= _f(c["wind_factor"])
                    note(
                        e0.domain,
                        e0.id,
                        f"Wind {w:.1f} m/s near the gap area",
                        _f(c["wind_factor"]),
                    )
    floor = max(0.0, min(1.0, _num(c["min_factor"]) or 0.0))
    return max(floor, min(1.0, factor)), notes, gaps
