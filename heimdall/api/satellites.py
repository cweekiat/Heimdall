"""Satellite positions and ground tracks for the globe view (read-only, GET only).

Elements are the CelesTrak `orbit_elements` events already in the Store (no new source, no new
host); positions are propagated locally with SGP4 at request time, so they are live to the second
while the elements themselves are hours to a day old (`epoch` is returned so the UI can say so).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query

from heimdall import config
from heimdall.orbits import ground_track, satrec_from_omm
from heimdall.ports import Store

ELEMENTS_MAX_AGE = timedelta(days=10)
CACHE_S = 10.0  # the UI interpolates along the track between polls
MAX_SATS = 300


def _sensors() -> dict[int, dict[str, str]]:
    sats = config.sources().get("sources", {}).get("celestrak", {}).get("satellites", {}) or {}
    out: dict[int, dict[str, str]] = {}
    for k, v in sats.items():
        try:
            out[int(k)] = {"name": str(v.get("name", k))[:40], "sensor": str(v.get("sensor", ""))}
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def satellite_positions(
    store: Store, now: datetime, past_min: int, next_min: int, step_s: float = 60.0
) -> list[dict[str, Any]]:
    latest: dict[int, Any] = {}
    for e in store.events_window(
        now - ELEMENTS_MAX_AGE, now + timedelta(days=1), ["orbit_elements"]
    ):
        try:
            norad = int(e.attrs.get("norad_id") or 0)
        except (TypeError, ValueError):
            continue
        if norad not in latest or e.ts > latest[norad].ts:
            latest[norad] = e
    meta = _sensors()
    out: list[dict[str, Any]] = []
    start = now - timedelta(minutes=past_min)
    n = int((past_min + next_min) * 60 / step_s) + 1
    for norad, e in sorted(latest.items())[:MAX_SATS]:
        omm = e.attrs.get("omm")
        if not isinstance(omm, dict):
            continue
        try:
            sat = satrec_from_omm(omm)
        except ValueError:
            continue
        track = ground_track(sat, start, n, step_s)
        here = ground_track(sat, now, 1, 1.0)[0]
        if here is None:
            continue
        m = meta.get(norad, {})
        out.append(
            {
                "norad_id": norad,
                "name": m.get("name") or str(e.attrs.get("name") or norad)[:40],
                "sensor": m.get("sensor", ""),
                "group": "Earth observation" if m else str(e.attrs.get("group") or "Other")[:40],
                "lat": here[0],
                "lon": here[1],
                "alt_km": here[2],
                "epoch": e.ts.isoformat(),
                # [lon, lat, alt_km]
                "past": [[p[1], p[0], p[2]] for p in track[: int(past_min * 60 / step_s) + 1] if p],
                "next": [[p[1], p[0], p[2]] for p in track[int(past_min * 60 / step_s) :] if p],
            }
        )
    return out


def satellites_router(store: Store) -> APIRouter:
    r = APIRouter()
    cache: dict[tuple[int, int], tuple[float, list[dict[str, Any]]]] = {}

    @r.get("/api/satellites")
    def satellites(
        past_min: int = Query(30, ge=0, le=180), next_min: int = Query(90, ge=0, le=360)
    ) -> dict[str, Any]:
        key = (past_min, next_min)
        hit = cache.get(key)
        if hit is None or time.monotonic() - hit[0] > CACHE_S:
            sats = satellite_positions(store, datetime.now(UTC), past_min, next_min)
            cache[key] = hit = (time.monotonic(), sats)
        return {"ts": datetime.now(UTC).isoformat(), "satellites": hit[1]}

    return r
