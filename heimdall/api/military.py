"""Military air activity API (task 039). Read-only.

GET /api/military -> {generated, window_min, contacts, airborne, on_ground, roles: {role: n},
    types: [{type, name, n}], countries: [{country, n}], high_interest: [aircraft], theatres:
    [{name, lat, lon, radius_km, roles: {role: n}}], aircraft: [{hex, flight, type, type_name,
    registration, role, country, operator, category, tags, lat, lon, alt, gs, track, ts,
    high_interest, known}]}
Computed from military rows in the store over the last `window_min` (default 10), newest per
aircraft, identity from plane-alert-db (ODbL) and the type code; cached 15 s.

GET /api/military/aircraft/{hex} (task 047) -> one aircraft investigated: identity, type reference
(config/mil_types.yaml), where it took off (OurAirports + its own track), what it may be doing now
(`milactivity`, with reasons), context (theatres, open alerts nearby) and its 72 h track.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query

from heimdall import config, milactivity, miltypes
from heimdall.fuse.fusion import ACTIVE
from heimdall.geo import KM_PER_NM, haversine_km
from heimdall.ingest import osm_bases, ourairports, plane_alert
from heimdall.military_step import _STATE, theatres
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


KIND_WORDS = {
    "gnss_jamming": "GPS jamming",
    "gnss_spoofing": "GPS spoofing",
    "spectrum_anomaly": "radio band far from normal",
    "mil_orbit": "military aircraft circling",
    "warning_area": "warning area",
}


def _track(store: Store, hex_: str, now: datetime) -> list[milactivity.Point]:
    """72 h decimated track (MilTracks, when the pipeline runs in this process) + recent fixes."""
    pts: dict[float, milactivity.Point] = {}
    st = _STATE[0] if _STATE else None
    if st is not None and st.store is store and hex_ in st.tracks.tracks:
        for pt in st.tracks.tracks[hex_].points:
            pts[pt[0]] = pt
    for o in store.aircraft_window(
        now - timedelta(hours=6), now + timedelta(seconds=1), ident=hex_
    ):
        t = o.ts.timestamp()
        alt = -1.0 if o.on_ground or o.alt_baro is None else float(o.alt_baro)
        pts[t] = (t, o.lat, o.lon, alt, float(o.gs) if o.gs is not None else -1.0)
    out = sorted(pts.values())
    thin: list[milactivity.Point] = []  # <= one point per 30 s keeps the response small
    for pt in out:
        if not thin or pt[0] - thin[-1][0] >= 30:
            thin.append(pt)
    return thin


def _mark_base(dep: dict[str, Any]) -> dict[str, Any]:
    """049: an OSM military base within 3 km of the take-off airfield marks it military."""
    f = dep.get("airfield")
    if f:
        hit = osm_bases.near(f["lat"], f["lon"], 3.0, osm_bases.bases())
        if hit:
            f["military"] = True
            f["base"] = hit[0]["name"]
    return dep


def bases_geojson(bbox: str | None) -> dict[str, Any]:
    """049: named military bases (OpenStreetMap, ODbL) as GeoJSON points, optional w,s,e,n box."""
    items = osm_bases.bases()
    if bbox:
        try:
            w, s_, e, n = (float(x) for x in bbox.split(","))
        except ValueError as ex:
            raise HTTPException(422, "bbox must be w,s,e,n") from ex
        items = [
            b
            for b in items
            if s_ <= b["lat"] <= n
            and (w <= b["lon"] <= e if w <= e else (b["lon"] >= w or b["lon"] <= e))
        ]
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [b["lon"], b["lat"]]},
                "properties": {
                    "id": b["id"],
                    "name": b["name"],
                    "kind": b["kind"],
                    "label": osm_bases.KINDS.get(b["kind"], "Military base"),
                    "operator": b.get("operator"),
                    "icao": b.get("icao"),
                },
            }
            for b in items[:6000]
        ],
        "total": len(items),
        "attribution": "© OpenStreetMap contributors (ODbL)",
    }


def investigate(store: Store, hex_: str, now: datetime) -> dict[str, Any]:
    pts = _track(store, hex_, now)
    if not pts:
        raise HTTPException(404, "no recent positions for this aircraft")
    summary = military_summary(store, now, 30)
    me = next((a for a in summary["aircraft"] if a["hex"] == hex_), None)
    rows = store.aircraft_window(now - timedelta(hours=6), now + timedelta(seconds=1), ident=hex_)
    type_code = (me or {}).get("type") or next(
        (o.type_code for o in reversed(rows) if o.type_code), None
    )
    ident = classify(hex_, type_code, plane_alert.index().get(hex_))
    airfields = ourairports.index()
    nearest = airfields.nearest
    mcfg = config.settings().detectors.get("military", {})
    ths = [
        {"name": t.name, "lat": t.lat, "lon": t.lon, "radius_km": t.radius_km}
        for t in theatres(mcfg, config.settings().aois)
    ]
    others = [
        {**a, "alt": a["alt"]}
        for a in summary["aircraft"]
        if a["hex"] != hex_ and not a["on_ground"]
    ]
    alerts = [
        {
            "ref": a.ref,
            "what": KIND_WORDS.get(a.kind.value, a.kind.value),
            "lat": a.lat,
            "lon": a.lon,
        }
        for a in store.list_alerts(since=now - timedelta(hours=24), status=list(ACTIVE), limit=500)
        if a.lat is not None and a.lon is not None
    ]
    rotary = ident.role == "rotary"
    act = milactivity.activity(pts, ident.role, now.timestamp(), nearest, ths, others, alerts)
    flight = milactivity.current_flight(pts)
    return {
        "hex": hex_,
        "flight": (me or {}).get("flight")
        or next((o.flight for o in reversed(rows) if o.flight), None),
        "registration": (me or {}).get("registration") or ident.registration,
        "type": type_code,
        "type_name": ident.type_name,
        "role": ident.role,
        "country": ident.country,
        "operator": ident.operator,
        "category": ident.category,
        "tags": list(ident.tags),
        "known": ident.known,
        "high_interest": ident.high_interest,
        "reference": miltypes.lookup(type_code),
        "departure": _mark_base(milactivity.departure(pts, nearest, rotary)),
        "activity": {
            "label": act.label,
            "confidence": act.confidence,
            "reasons": act.reasons,
            "context": act.context,
        },
        "last": {
            "ts": datetime.fromtimestamp(pts[-1][0], UTC).isoformat(),
            "lat": pts[-1][1],
            "lon": pts[-1][2],
            "alt": None if pts[-1][3] < 0 else pts[-1][3],
            "gs": None if pts[-1][4] < 0 else pts[-1][4],
        },
        # [lon, lat, alt ft or -1, epoch s]; flight_from = index where the current flight starts
        "track": [[round(p[2], 5), round(p[1], 5), p[3], int(p[0])] for p in pts],
        "flight_from": max(0, len(pts) - len(flight)),
        "airfields_loaded": airfields.n > 0,
        "limits": "Only what ADS-B receivers heard: aircraft can fly dark, receivers end offshore, "
        "and intent is never known. Type figures are rounded public data, not this airframe.",
        "attribution": "plane-alert-db (ODbL); airfields: OurAirports (public domain)",
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

    @r.get("/api/military/bases")
    def bases(bbox: str | None = None) -> dict[str, Any]:
        return bases_geojson(bbox)

    @r.get("/api/military/aircraft/{hex_id}")
    def aircraft(hex_id: str = Path(pattern=r"^[0-9a-fA-F]{6}$")) -> dict[str, Any]:
        return investigate(store, hex_id.lower(), datetime.now(UTC))

    return r
