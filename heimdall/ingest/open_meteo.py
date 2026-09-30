"""Open-Meteo live forecast API (with past_days) -> `weather` confounder GeoEvents (ADR-007).

Uses api.open-meteo.com, not archive-api (not live). Confounders only lower alert confidence; they
never create alerts. No key. One event per grid point per hour.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from heimdall.config import AOI, Settings
from heimdall.geo import bbox_for_radius, h3_cell, within_radius_nm
from heimdall.ingest.poll import PollConnector, Unit, num, parse_dt
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

HOURLY = ("cloud_cover", "precipitation", "wind_speed_10m", "weather_code")
BATCH = 50  # coordinates per request
_TO_MS = {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 0.514444}


def parse_open_meteo(payload: Mapping[str, Any] | Sequence[Any]) -> tuple[list[GeoEvent], int]:
    """(events, n_dropped) for one location object or a list of them (multi-location response)."""
    out: list[GeoEvent] = []
    dropped = 0
    for loc in payload if isinstance(payload, list) else [payload]:
        if not isinstance(loc, Mapping):
            dropped += 1
            continue
        evs, bad = _parse_location(loc)
        out.extend(evs)
        dropped += bad
    return out, dropped


def _parse_location(loc: Mapping[str, Any]) -> tuple[list[GeoEvent], int]:
    lat, lon = num(loc.get("latitude")), num(loc.get("longitude"))
    hourly = loc.get("hourly")
    if lat is None or lon is None or not isinstance(hourly, Mapping):
        return [], 1
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return [], 1
    times = hourly.get("time")
    cols: dict[str, list[Any]] = {}
    for k in HOURLY:
        c = hourly.get(k)
        if not isinstance(c, list):
            return [], 1
        cols[k] = c
    if not isinstance(times, list):
        return [], 1
    units = loc.get("hourly_units")
    unit = str(units.get("wind_speed_10m", "km/h")) if isinstance(units, Mapping) else "km/h"
    factor = _TO_MS.get(unit)
    if factor is None:
        return [], 1
    out: list[GeoEvent] = []
    dropped = 0
    for i, t in enumerate(times):
        ts = parse_dt(t)
        vals = {k: num(c[i]) if i < len(c) else None for k, c in cols.items()}
        cloud = vals["cloud_cover"]
        if ts is None or cloud is None:  # null hour: skipped, not an error
            continue
        if not 0 <= cloud <= 100 or any(
            v is not None and v < 0 for k, v in vals.items() if k != "weather_code"
        ):
            dropped += 1
            continue
        wind = vals["wind_speed_10m"]
        code = vals["weather_code"]
        out.append(
            GeoEvent(
                id=f"open_meteo|{lat:.2f},{lon:.2f}|{ts.strftime('%Y%m%dT%H')}",
                ts=ts,
                source="open_meteo",
                domain=Domain.EVENTS,
                kind="weather",
                lat=lat,
                lon=lon,
                valid_from=ts,
                valid_to=ts + timedelta(hours=1),
                title="weather",
                value=cloud,
                attrs={
                    "role": "confounder",
                    "cloud_cover": cloud,
                    "precipitation_mm": vals["precipitation"],
                    "wind_speed_ms": round(wind * factor, 3) if wind is not None else None,
                    "weather_code": int(code) if code is not None else None,
                    "grid_lat": lat,
                    "grid_lon": lon,
                },
                h3_r4=h3_cell(lat, lon, 4),
                h3_r6=h3_cell(lat, lon, 6),
            )
        )
    return out, dropped


def grid_points(aois: Sequence[AOI], grid_deg: float) -> list[tuple[float, float]]:
    """Unique grid points (multiples of grid_deg) inside any AOI circle."""
    pts: set[tuple[float, float]] = set()
    for a in aois:
        for s, w, n, e in bbox_for_radius(a.lat, a.lon, a.radius_nm):
            if e < w:  # never happens for boxes from bbox_for_radius; guard anyway
                continue
            i0, i1 = math.ceil(s / grid_deg), math.floor(n / grid_deg)
            j0, j1 = math.ceil(w / grid_deg), math.floor(e / grid_deg)
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    lat, lon = round(i * grid_deg, 4), round(j * grid_deg, 4)
                    if within_radius_nm(lat, lon, a.lat, a.lon, a.radius_nm):
                        pts.add((lat, lon))
    return sorted(pts)


class OpenMeteoConnector(PollConnector):
    domain = Domain.EVENTS

    def __init__(
        self,
        name: str,
        base_url: str,
        aois: Sequence[AOI],
        *,
        grid_deg: float = 0.5,
        lookback_days: int = 7,
        past_days: int = 2,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.base_url = base_url.rstrip("/")
        self.aois = list(aois)
        self.grid_deg = grid_deg
        self.lookback_days = lookback_days
        self.past_days = max(0, min(past_days, lookback_days))

    def units(self) -> list[Unit]:
        pts = grid_points(self.aois, self.grid_deg)
        return [self._unit(pts[i : i + BATCH]) for i in range(0, len(pts), BATCH)]

    def _unit(self, pts: list[tuple[float, float]]) -> Unit:
        async def run() -> tuple[list[GeoEvent], int]:
            params = {
                "latitude": ",".join(f"{p[0]:.2f}" for p in pts),
                "longitude": ",".join(f"{p[1]:.2f}" for p in pts),
                "hourly": ",".join(HOURLY),
                "past_days": self.past_days,
                "forecast_days": 1,
                "timezone": "GMT",
            }
            resp = await self.client.get(f"{self.base_url}/forecast", params=params)
            events, dropped = parse_open_meteo(resp.json())
            cutoff = self._now() - timedelta(days=self.lookback_days)
            return [e for e in events if e.ts >= cutoff], dropped

        return run


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return OpenMeteoConnector(
        key,
        str(cfg["base"]),
        settings.aois,
        grid_deg=float(cfg.get("grid_deg", 0.5)),
        lookback_days=int(cfg.get("lookback_days", 7)),
        past_days=int(cfg.get("past_days", 2)),
        poll_s=float(cfg.get("poll_s", 21600)),
        rps=float(cfg.get("rps", 0.5)),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 3600)),
    )
