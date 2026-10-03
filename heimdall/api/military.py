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
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query

from heimdall import config, milactivity, miltypes, theatre_index
from heimdall.detect import military as mil_detect
from heimdall.detect.warning_area import NOTICE_KINDS
from heimdall.fuse.corroborate import CORROBORATING_CATEGORIES
from heimdall.fuse.fusion import ACTIVE
from heimdall.geo import KM_PER_NM, haversine_km
from heimdall.ingest import osm_bases, ourairports, plane_alert
from heimdall.military_step import _STATE, theatres
from heimdall.milroles import STRATEGIC, classify
from heimdall.ports import Store
from heimdall.schemas import AircraftObs, AlertKind

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


def _track(
    store: Store, hex_: str, now: datetime, lookback_h: float = 6.0
) -> list[milactivity.Point]:
    """72 h decimated track (MilTracks, when the pipeline runs in this process) + recent fixes."""
    pts: dict[float, milactivity.Point] = {}
    st = _STATE[0] if _STATE else None
    if st is not None and st.store is store and hex_ in st.tracks.tracks:
        for pt in st.tracks.tracks[hex_].points:
            pts[pt[0]] = pt
    for o in store.aircraft_window(
        now - timedelta(hours=lookback_h), now + timedelta(seconds=1), ident=hex_
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


def entity_info(
    store: Store, hexes: list[str], start: datetime, end: datetime
) -> dict[str, dict[str, Any]]:
    """Callsign / type / role / operator per ICAO hex for alert details (affected aircraft):
    newest position in [start, end] (the alert's time span), the 72 h military tracks and
    plane-alert-db (type and registration when nothing was heard)."""
    hexes = [x.lower() for x in hexes if len(x) == 6][:300]
    if not hexes:
        return {}
    latest: dict[str, AircraftObs] = {}
    for o in store.aircraft_window(start, end, ident=frozenset(hexes)):
        if o.hex not in latest or o.ts >= latest[o.hex].ts:
            latest[o.hex] = o
    st = _STATE[0] if _STATE and _STATE[0].store is store else None
    db = plane_alert.index()
    out: dict[str, dict[str, Any]] = {}
    for x in hexes:
        ob = latest.get(x)
        tr = st.tracks.tracks.get(x) if st is not None else None
        flight = ((ob.flight if ob else None) or (tr.flight if tr else None) or "").strip() or None
        tc = (
            (ob.type_code if ob else None)
            or (tr.type_code if tr else None)
            or ((db.get(x) or {}).get("icao_type") or None)
        )
        mil = bool(ob.mil) if ob else (tr is not None or x in db)
        i = classify(x, tc, db.get(x))
        reg = (
            (ob.registration if ob else None) or (tr.registration if tr else None) or i.registration
        )
        out[x] = {
            "flight": flight,
            "registration": reg,
            "type": tc,
            "type_name": i.type_name,
            "role": i.role if mil else None,
            "operator": i.operator,
            "country": i.country,
            "mil": mil,
        }
    return out


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


def investigate(store: Store, hex_: str, now: datetime, lookback_h: float = 6.0) -> dict[str, Any]:
    pts = [p for p in _track(store, hex_, now, lookback_h) if p[0] <= now.timestamp() + 1]
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


MIL_ALERTS = {
    AlertKind.MIL_DEPLOYMENT,
    AlertKind.MIL_SURGE,
    AlertKind.MIL_HIGH_INTEREST,
    AlertKind.MIL_BUILDUP,
}
GNSS_ALERTS = {AlertKind.GNSS_JAMMING, AlertKind.GNSS_SPOOFING, AlertKind.AIS_SPOOFING}


def theatres_index(store: Store, now: datetime) -> dict[str, Any]:
    """051: per-theatre activity components vs that theatre's own normal (see theatre_index)."""
    mcfg = config.settings().detectors.get("military", {})
    ths = theatres(mcfg, config.settings().aois)
    alerts = store.list_alerts(since=now - timedelta(hours=72), status=list(ACTIVE), limit=5000)
    conflict = [
        e
        for e in store.events_window(now - timedelta(days=8), now, ["conflict_event"])
        if int(e.attrs.get("sources") or 0) >= 2 and e.lat is not None and e.lon is not None
    ]
    gdelt_days = {e.ts.date() for e in conflict}
    notices = [
        e
        for e in store.events_window(now - timedelta(days=30), now, list(NOTICE_KINDS))
        if e.valid_from is not None
        and e.valid_from >= now - timedelta(hours=24)
        and str(e.attrs.get("category") or "") in (*CORROBORATING_CATEGORIES, "airspace")
    ]
    since = now - timedelta(days=float(mcfg.get("baseline_days", 14)))
    out = []
    for th in ths:

        def inside(lat: float | None, lon: float | None, th: Any = th) -> bool:
            return (
                lat is not None
                and lon is not None
                and haversine_km(lat, lon, th.lat, th.lon) <= th.radius_km
            )

        by_hour: dict[datetime, float] = {}
        for role in STRATEGIC:
            for ts, v in store.metric_history(f"{th.name}|{role}", mil_detect.METRIC, since):
                by_hour[ts] = by_hour.get(ts, 0.0) + v
        hours = sorted(by_hour)
        now_v = by_hour[hours[-1]] if hours and hours[-1] >= now - timedelta(hours=2) else 0.0
        hist = [by_hour[h] for h in hours[:-1]]
        mine = [e for e in conflict if inside(e.lat, e.lon)]
        today = sum(1 for e in mine if e.ts >= now - timedelta(hours=24))
        days = [
            sum(1 for e in mine if now - timedelta(days=d + 1) <= e.ts < now - timedelta(days=d))
            for d in range(1, 8)
            if (now - timedelta(days=d)).date() in gdelt_days
        ]
        idx = theatre_index.build(
            {"name": th.name, "lat": th.lat, "lon": th.lon, "radius_km": th.radius_km},
            now_v,
            hist,
            [a.ref or a.id[:8] for a in alerts if a.kind in MIL_ALERTS and a.aoi == th.name],
            [a.ref or a.id[:8] for a in alerts if a.kind in GNSS_ALERTS and inside(a.lat, a.lon)],
            [
                a.ref or a.id[:8]
                for a in alerts
                if a.kind == AlertKind.SPECTRUM_ANOMALY and inside(a.lat, a.lon)
            ],
            today,
            days,
            sum(1 for n in notices if inside(n.lat, n.lon)),
        )
        out.append(asdict(idx))
    out.sort(key=lambda t: (-t["level"], t["name"]))
    return {
        "generated": now.isoformat(),
        "theatres": out,
        "note": "Counts of what Heimdall observed against each theatre's own normal. Not a threat "
        "level; it predicts nothing and names no target.",
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

    idx_cache: list[tuple[float, dict[str, Any]]] = []

    @r.get("/api/military/theatres/index")
    def theatres_idx() -> dict[str, Any]:
        if idx_cache and time.monotonic() - idx_cache[0][0] < 60:
            return idx_cache[0][1]
        out = theatres_index(store, datetime.now(UTC))
        idx_cache[:] = [(time.monotonic(), out)]
        return out

    @r.get("/api/military/bases")
    def bases(bbox: str | None = None) -> dict[str, Any]:
        return bases_geojson(bbox)

    @r.get("/api/military/aircraft/{hex_id}")
    def aircraft(
        hex_id: str = Path(pattern=r"^[0-9a-fA-F]{6}$"), at: datetime | None = None
    ) -> dict[str, Any]:
        """`at` (054): investigate as of a past moment, e.g. an alert's last-seen time."""
        now = datetime.now(UTC)
        if at is not None:
            at = at if at.tzinfo else at.replace(tzinfo=UTC)
            now = min(now, at)
        # a past moment has no live 72 h track state: read 72 h of stored positions instead
        return investigate(store, hex_id.lower(), now, 6.0 if at is None else 72.0)

    return r
