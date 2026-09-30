"""Live traffic + notice endpoints (task 012a). GeoJSON, latest fix per entity, capped.

Only Store-port calls are used; free text (notice bodies) is passed through as data and flagged
`untrusted: true`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query

from heimdall import config
from heimdall.geo import BBox, h3_cell, haversine_km, in_bbox
from heimdall.ports import Store
from heimdall.schemas import AircraftObs, AlertStatus, VesselObs

CAP = 5000
ZOOM_CAP = 20000  # positions returned when the client sends `zoom`
DENSITY_BELOW_ZOOM = 5.0  # below this the API returns H3 density cells, not positions
NOTICE_KINDS = ["nav_warning", "notam"]
NOTICE_LOOKBACK = timedelta(days=30)
TEXT_MAX = 2000
_OPEN = (AlertStatus.NEW, AlertStatus.ACKNOWLEDGED, AlertStatus.UNDER_REVIEW)
AT_MIN = datetime(2000, 1, 1, tzinfo=UTC)
AT_MAX = datetime(2100, 1, 1, tzinfo=UTC)
_FUTURE = timedelta(minutes=5)  # tolerate small clock skew on fix timestamps


def parse_bbox(raw: str) -> BBox:
    try:
        w, s, e, n = (float(x) for x in raw.split(","))
    except ValueError as err:
        raise HTTPException(422, "bbox must be w,s,e,n") from err
    if not all(math.isfinite(v) for v in (w, s, e, n)):
        raise HTTPException(422, "bbox must be finite")
    if not (-90 <= s < n <= 90 and -180 <= w <= 180 and -180 <= e <= 180):
        raise HTTPException(422, "bbox out of range")
    return w, s, e, n


def _source_filter(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    keys = {k.strip().lower() for k in raw.split(",") if k.strip()}
    if not all(re.fullmatch(r"[a-z0-9_.]{1,40}", k) for k in keys):
        raise HTTPException(422, "invalid sources")
    return {k.replace(".", "_") for k in keys}


def _newest(
    rows: Iterable[Any], key: Callable[[Any], Any], want: set[str] | None
) -> dict[Any, datetime]:
    out: dict[Any, datetime] = {}
    for r in rows:
        if want is not None and _pkey(r.provider) not in want:
            continue
        k = key(r)
        if k not in out or r.ts > out[k]:
            out[k] = r.ts
    return out


def _pkey(provider: str) -> str:
    return provider.lower().replace(".", "_")


def ship_class(ship_type: int | None) -> str:
    if ship_type is None:
        return "other"
    if ship_type == 35:
        return "military"
    if ship_type == 30:
        return "fishing"
    if 60 <= ship_type <= 69:
        return "passenger"
    if 70 <= ship_type <= 79:
        return "cargo"
    if 80 <= ship_type <= 89:
        return "tanker"
    return "other"


def gps_bad(o: AircraftObs) -> bool:
    return (o.nic is not None and o.nic < 7) or (o.nac_p is not None and o.nac_p < 8)


def _fc(features: list[dict[str, Any]], total: int, cap: int = CAP) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features, "truncated": total > cap}


def density(points: Iterable[tuple[float, float, bool]], zoom: float) -> dict[str, Any]:
    """H3 density cells (r3 below zoom 3, else r4): count and military count per cell."""
    res = 3 if zoom < 3 else 4
    cells: dict[str, list[int]] = {}
    total = 0
    for lat, lon, mil in points:
        c = cells.setdefault(h3_cell(lat, lon, res), [0, 0])
        c[0] += 1
        c[1] += int(mil)
        total += 1
    return {
        "type": "density",
        "res": res,
        "total": total,
        "cells": [{"h3": k, "count": v[0], "mil": v[1]} for k, v in cells.items()],
    }


def _point(lat: float, lon: float, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def _centroid(geom: dict[str, Any] | None) -> tuple[float, float] | None:
    pts = list(_coords(geom.get("coordinates") if geom else None))
    if not pts:
        return None
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


def _coords(c: Any) -> Iterable[tuple[float, float]]:
    if isinstance(c, list | tuple) and len(c) >= 2 and all(isinstance(v, int | float) for v in c):
        yield float(c[0]), float(c[1])
    elif isinstance(c, list | tuple):
        for x in c:
            yield from _coords(x)


def traffic_router(store: Store, now: Callable[[], datetime] | None = None) -> APIRouter:
    r = APIRouter()
    clock = now or (lambda: datetime.now(UTC))

    cache: dict[str, Any] = {"key": None, "ids": set()}

    def in_alert_entities() -> set[str]:
        # One list_alerts per poll; the evidence lookup is redone only when the open-alert set
        # (ids, status, evidence ids) changes.
        t = clock()
        alerts = store.list_alerts(since=t - timedelta(hours=24), status=list(_OPEN), limit=1000)
        key = tuple((a.id, a.status, tuple(a.evidence_ids)) for a in alerts)
        if cache["key"] != key:
            ev_ids = sorted({i for a in alerts for i in a.evidence_ids})
            cache["ids"] = {e.lower() for ev in store.get_evidence(ev_ids) for e in ev.entities}
            cache["key"] = key
        return cache["ids"]

    @r.get("/api/traffic/air")
    def air(
        bbox: str,
        max_age_s: int = Query(60, ge=1, le=86400),
        sources: str | None = None,
        zoom: float | None = Query(None, ge=0, le=24),
    ) -> dict[str, Any]:
        bb, want, t = parse_bbox(bbox), _source_filter(sources), clock()
        rows = store.aircraft_window(t - timedelta(seconds=max_age_s), t + _FUTURE, bbox=bb)
        latest: dict[str, AircraftObs] = {}
        provs: dict[str, set[str]] = {}
        for o in sorted(rows, key=lambda x: x.ts):
            if want is not None and _pkey(o.provider) not in want:
                continue
            latest[o.hex] = o
            provs.setdefault(o.hex, set()).add(o.provider)
        # bbox pushdown only saw in-box fixes: drop entities whose newest fix is elsewhere.
        if latest:
            newest = _newest(
                store.aircraft_window(
                    t - timedelta(seconds=max_age_s), t + _FUTURE, ident=frozenset(latest)
                ),
                lambda o: o.hex,
                want,
            )
            latest = {k: v for k, v in latest.items() if newest[k] == v.ts}
        hot = in_alert_entities()
        picked = sorted(
            (o for o in latest.values() if in_bbox(o.lat, o.lon, bb)),
            key=lambda x: x.ts,
            reverse=True,
        )
        if zoom is not None and zoom < DENSITY_BELOW_ZOOM:
            return density(((o.lat, o.lon, o.mil) for o in picked), zoom)
        cap = ZOOM_CAP if zoom is not None else CAP
        feats = [
            _point(
                o.lat,
                o.lon,
                {
                    "hex": o.hex,
                    "flight": (o.flight or "").strip() or None,
                    "registration": o.registration,
                    "type_code": o.type_code,
                    "mil": o.mil,
                    "alt_baro": o.alt_baro,
                    "gs": o.gs,
                    "track": o.track,
                    "on_ground": o.on_ground,
                    "provider": o.provider,
                    "providers": sorted(provs[o.hex]),
                    "age_s": max(0.0, round((t - o.ts).total_seconds(), 1)),
                    "gps_bad": gps_bad(o),
                    "in_alert": o.hex.lower() in hot,
                },
            )
            for o in picked[:cap]
        ]
        return _fc(feats, len(picked), cap)

    @r.get("/api/traffic/sea")
    def sea(
        bbox: str,
        max_age_s: int = Query(900, ge=1, le=86400),
        sources: str | None = None,
        zoom: float | None = Query(None, ge=0, le=24),
    ) -> dict[str, Any]:
        bb, want, t = parse_bbox(bbox), _source_filter(sources), clock()
        rows = store.vessels_window(t - timedelta(seconds=max_age_s), t + _FUTURE, bbox=bb)
        latest: dict[int, VesselObs] = {}
        for v in sorted(rows, key=lambda x: x.ts):
            if want is None or _pkey(v.provider) in want:
                latest[v.mmsi] = v
        if latest:
            newest = _newest(
                store.vessels_window(
                    t - timedelta(seconds=max_age_s), t + _FUTURE, ident=frozenset(latest)
                ),
                lambda v: v.mmsi,
                want,
            )
            latest = {k: v for k, v in latest.items() if newest[k] == v.ts}
        hot = in_alert_entities()
        picked = sorted(
            (v for v in latest.values() if in_bbox(v.lat, v.lon, bb)),
            key=lambda x: x.ts,
            reverse=True,
        )
        if zoom is not None and zoom < DENSITY_BELOW_ZOOM:
            return density(
                ((v.lat, v.lon, ship_class(v.ship_type) == "military") for v in picked), zoom
            )
        cap = ZOOM_CAP if zoom is not None else CAP
        feats = [
            _point(
                v.lat,
                v.lon,
                {
                    "mmsi": v.mmsi,
                    "name": v.name,
                    "ship_type": v.ship_type,
                    "ship_class": ship_class(v.ship_type),
                    "sog": v.sog,
                    "cog": v.cog,
                    "heading": v.heading,
                    "destination": v.destination,
                    "provider": v.provider,
                    "age_s": max(0.0, round((t - v.ts).total_seconds(), 1)),
                    "in_alert": str(v.mmsi) in hot,
                },
            )
            for v in picked[:cap]
        ]
        return _fc(feats, len(picked), cap)

    def _trail(coords: list[list[float]], props: dict[str, Any]) -> dict[str, Any]:
        if not coords:
            raise HTTPException(404, "no recent positions")
        if len(coords) == 1:
            coords = coords * 2
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": props,
        }

    @r.get("/api/traffic/air/{hex_id}/trail")
    def air_trail(
        hex_id: str = Path(pattern=r"^~?[0-9a-fA-F]{6}$"), min: int = Query(30, ge=1, le=1440)
    ) -> dict[str, Any]:
        t, h = clock(), hex_id.lower()
        rows = store.aircraft_window(t - timedelta(minutes=min), t + _FUTURE, ident=h)
        rows.sort(key=lambda o: o.ts)
        return _trail([[o.lon, o.lat] for o in rows], {"hex": h, "n": len(rows)})

    @r.get("/api/traffic/sea/{mmsi}/trail")
    def sea_trail(
        mmsi: int = Path(ge=0, le=999_999_999), min: int = Query(120, ge=1, le=1440)
    ) -> dict[str, Any]:
        t = clock()
        rows = store.vessels_window(t - timedelta(minutes=min), t + _FUTURE, ident=mmsi)
        rows.sort(key=lambda v: v.ts)
        return _trail([[v.lon, v.lat] for v in rows], {"mmsi": mmsi, "n": len(rows)})

    @r.get("/api/notices")
    def notices(at: str | None = None, aoi: str | None = None) -> dict[str, Any]:
        t = clock()
        if at:
            try:
                t = datetime.fromisoformat(at.replace("Z", "+00:00"))
            except ValueError as e:
                raise HTTPException(422, f"invalid at: {at}") from e
            t = t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC)
            if not AT_MIN <= t <= AT_MAX:
                raise HTTPException(422, "at out of range")
        area = None
        if aoi:
            match = [a for a in config.settings().aois if a.name == aoi]
            if not match:
                raise HTTPException(404, "unknown aoi")
            area = match[0]
        feats: list[dict[str, Any]] = []
        events = store.events_window(t - NOTICE_LOOKBACK, t + timedelta(seconds=1), NOTICE_KINDS)
        for ev in sorted(events, key=lambda e: e.ts, reverse=True):
            if ev.attrs.get("status") == "cancelled":
                continue
            if (ev.valid_from and ev.valid_from > t) or (ev.valid_to and ev.valid_to < t):
                continue
            c = _centroid(ev.geometry) or (
                (ev.lat, ev.lon) if ev.lat is not None and ev.lon is not None else None
            )
            if area is not None and (
                c is None or haversine_km(c[0], c[1], area.lat, area.lon) > area.radius_nm * 1.852
            ):
                continue
            geom = ev.geometry or (
                {"type": "Point", "coordinates": [ev.lon, ev.lat]} if c else None
            )
            feats.append(
                {
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {
                        "id": ev.id,
                        "source": ev.source,
                        "kind": ev.kind,
                        "category": ev.attrs.get("category"),
                        "ref": ev.attrs.get("ref"),
                        "title": ev.title,
                        "valid_from": ev.valid_from.isoformat() if ev.valid_from else None,
                        "valid_to": ev.valid_to.isoformat() if ev.valid_to else None,
                        "url": ev.url,
                        "parse_status": ev.attrs.get("parse_status"),
                        "alt_lower_ft": ev.attrs.get("alt_lower_ft"),
                        "alt_upper_ft": ev.attrs.get("alt_upper_ft"),
                        "text": (ev.text or "")[:TEXT_MAX],
                        "untrusted": True,
                    },
                }
            )
        return _fc(feats[:CAP], len(feats))

    return r
