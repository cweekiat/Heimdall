"""Military air activity API (task 039). Read-only.

GET /api/military -> {generated, window_min, contacts, airborne, on_ground, roles: {role: n},
    types: [{type, name, n}], countries: [{country, n}], high_interest: [aircraft], theatres:
    [{name, lat, lon, radius_km, roles: {role: n}}], aircraft: [{hex, flight, type, type_name,
    registration, role, country, operator, category, tags, lat, lon, alt, gs, track, ts,
    high_interest, known}]}
Computed from military rows in the store over the last `window_min` (default 10), newest per
aircraft, identity from plane-alert-db (ODbL) and the type code; cached 15 s.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query

from heimdall import config
from heimdall.geo import KM_PER_NM, haversine_km
from heimdall.ingest import plane_alert
from heimdall.military_step import theatres
from heimdall.milroles import classify
from heimdall.ports import Store
from heimdall.schemas import AircraftObs

CACHE_S = 15.0


def military_summary(store: Store, now: datetime, window_min: int = 10) -> dict[str, Any]:
    rows = store.aircraft_window(
        now - timedelta(minutes=window_min), now + timedelta(seconds=1), mil_only=True
    )
    latest: dict[str, AircraftObs] = {}
    for o in rows:
        if o.hex not in latest or o.ts >= latest[o.hex].ts:
            latest[o.hex] = o
    db = plane_alert.index()
    aircraft: list[dict[str, Any]] = []
    for h, o in sorted(latest.items()):
        i = classify(h, o.type_code, db.get(h))
        aircraft.append(
            {
                "hex": h,
                "flight": o.flight,
                "type": o.type_code,
                "type_name": i.type_name,
                "registration": o.registration or i.registration,
                "role": i.role,
                "country": i.country,
                "operator": i.operator,
                "category": i.category,
                "tags": list(i.tags),
                "lat": round(o.lat, 4),
                "lon": round(o.lon, 4),
                "alt": None if o.on_ground else o.alt_baro,
                "on_ground": o.on_ground,
                "gs": o.gs,
                "track": o.track,
                "ts": o.ts.isoformat(),
                "high_interest": i.high_interest,
                "known": i.known,
            }
        )
    mcfg = config.settings().detectors.get("military", {})
    ths = []
    for th in theatres(mcfg, config.settings().aois):
        inside = [
            a for a in aircraft if haversine_km(a["lat"], a["lon"], th.lat, th.lon) <= th.radius_km
        ]
        ths.append(
            {
                "name": th.name,
                "lat": th.lat,
                "lon": th.lon,
                "radius_km": th.radius_km,
                "radius_nm": round(th.radius_km / KM_PER_NM, 1),
                "n": len(inside),
                "roles": dict(Counter(a["role"] for a in inside)),
            }
        )
    types = Counter((a["type"] or "?", a["type_name"]) for a in aircraft)
    return {
        "generated": now.isoformat(),
        "window_min": window_min,
        "contacts": len(aircraft),
        "airborne": sum(1 for a in aircraft if not a["on_ground"]),
        "on_ground": sum(1 for a in aircraft if a["on_ground"]),
        "identified": sum(1 for a in aircraft if a["known"]),
        "roles": dict(Counter(a["role"] for a in aircraft).most_common()),
        "types": [{"type": t, "name": nm, "n": n} for (t, nm), n in types.most_common(15)],
        "countries": [
            {"country": c, "n": n}
            for c, n in Counter(a["country"] or "Unknown" for a in aircraft).most_common(15)
        ],
        "high_interest": [a for a in aircraft if a["high_interest"]],
        "theatres": ths,
        "aircraft": aircraft,
        "attribution": "Aircraft identities: plane-alert-db (sdr-enthusiasts), ODbL 1.0",
    }


def military_router(store: Store) -> APIRouter:
    r = APIRouter()
    cache: dict[int, tuple[float, dict[str, Any]]] = {}

    @r.get("/api/military")
    def military(window_min: int = Query(10, ge=1, le=60)) -> dict[str, Any]:
        hit = cache.get(window_min)
        if hit is not None and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        out = military_summary(store, datetime.now(UTC), window_min)
        cache[window_min] = (time.monotonic(), out)
        return out

    return r
