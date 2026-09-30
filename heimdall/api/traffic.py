"""Live traffic + notice endpoints (task 012a). GeoJSON, latest fix per entity, capped.

Only Store-port calls are used; free text (notice bodies) is passed through as data and flagged
`untrusted: true`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import JSONResponse, Response

from heimdall import config
from heimdall.geo import BBox, h3_cell, haversine_km, in_bbox
from heimdall.live import Entry, Layer, LivePositions
from heimdall.ports import Store
from heimdall.schemas import AircraftObs, AlertStatus, Band, VesselObs

CAP = 5000
COVERAGE_WINDOW_S = 3600
ZOOM_CAP = 20000  # positions returned when the client sends `zoom`
DENSITY_BELOW_ZOOM = 5.0  # below this the API returns H3 density cells, not positions


def density_res(zoom: float) -> int:
    """H3 resolution for density hexes: r3 (~60 km) < z2.5, r4 (~22 km) < z4, r5 (~8.5 km) above."""
    return 3 if zoom < 2.5 else 4 if zoom < 4 else 5
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
    return {
        "type": "FeatureCollection",
        "features": features,
        "truncated": total > cap,
        "total": total,
    }


def _json(body: dict[str, Any]) -> JSONResponse:
    """Serialise directly: skips FastAPI's per-element jsonable_encoder (slow for 20k points)."""
    return JSONResponse(body)


_Rows = Sequence[tuple[Any, Iterable[str]]]  # (AircraftObs | VesselObs, providers)


def _frag(kind: str, e: Entry) -> str:
    """Serialised feature up to (excluding) the per-request fields; cached on the entry."""
    o: Any = e.obs
    if kind == "air":
        props: dict[str, Any] = {
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
            "providers": sorted(e.providers),
            "gps_bad": e.gps_bad,
        }
    else:
        props = {
            "mmsi": o.mmsi,
            "name": o.name,
            "ship_type": o.ship_type,
            "ship_class": ship_class(o.ship_type),
            "sog": o.sog,
            "cog": o.cog,
            "heading": o.heading,
            "destination": o.destination,
            "provider": o.provider,
        }
    return (
        '{"type":"Feature","geometry":{"type":"Point","coordinates":'
        f'[{o.lon!r},{o.lat!r}]}},"properties":{json.dumps(props)[:-1]}'
    )


AIR_STATIC = (
    "hex",
    "flight",
    "registration",
    "type_code",
    "mil",
    "alt_baro",
    "gs",
    "track",
    "on_ground",
    "provider",
    "providers",
    "gps_bad",
)
SEA_STATIC = (
    "mmsi",
    "name",
    "ship_type",
    "ship_class",
    "sog",
    "cog",
    "heading",
    "destination",
    "provider",
)


def _static(kind: str, e: Entry) -> dict[str, Any]:
    o: Any = e.obs
    if kind == "air":
        return {
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
            "providers": sorted(e.providers),
            "gps_bad": e.gps_bad,
        }
    return {
        "mmsi": o.mmsi,
        "name": o.name,
        "ship_type": o.ship_type,
        "ship_class": ship_class(o.ship_type),
        "sog": o.sog,
        "cog": o.cog,
        "heading": o.heading,
        "destination": o.destination,
        "provider": o.provider,
    }


def _emit_compact_fast(
    kind: str,
    ents: Sequence[Entry],
    t: datetime,
    hot_fn: Callable[[], set[str]],
    cap: int,
    extra: dict[str, Any],
) -> Response:
    """Columnar response; per-entry static values are cached and transposed with zip."""
    total, hot = len(ents), hot_fn()
    picked = ents[:cap]
    names = AIR_STATIC if kind == "air" else SEA_STATIC
    rows: list[tuple[Any, ...]] = []
    for e in picked:
        r = e.row
        if r is None:
            d = _static(kind, e)
            r = e.row = tuple(d[k] for k in names)
        rows.append(r)
    tt = t.timestamp()
    cols: dict[str, Any] = (
        dict(zip(names, zip(*rows, strict=True), strict=True)) if rows else {k: () for k in names}
    )
    cols["lon"] = [e.lon for e in picked]
    cols["lat"] = [e.lat for e in picked]
    cols["age_s"] = [max(0.0, round(tt - e.ts.timestamp(), 1)) for e in picked]
    if kind == "air":
        cols["in_alert"] = [e.key.lower() in hot for e in picked]
    else:
        cols["in_alert"] = [str(e.key) in hot for e in picked]
    return _json(
        {
            "type": "compact",
            "kind": kind,
            "n": len(picked),
            "truncated": total > cap,
            "total": total,
            "now": t.isoformat(),
            **extra,
            "lat": [e.lat for e in picked],
            "lon": [e.lon for e in picked],
            "cols": cols,
        }
    )


def _emit_fast(
    kind: str,
    ents: Sequence[Entry],
    t: datetime,
    hot_fn: Callable[[], set[str]],
    cap: int,
    extra: dict[str, Any],
) -> Response:
    """GeoJSON from cached per-entry fragments: only age_s / in_alert are formatted per request."""
    total, hot = len(ents), hot_fn()
    tt = t.timestamp()
    feats: list[str] = []
    for e in ents[:cap]:
        frag = e.frag
        if frag is None:
            frag = e.frag = _frag(kind, e)
        alert = (e.key.lower() in hot) if kind == "air" else (str(e.key) in hot)
        age = max(0.0, round(tt - e.ts.timestamp(), 1))
        feats.append(f'{frag},"age_s":{age},"in_alert":{"true" if alert else "false"}}}}}')
    head = json.dumps({"truncated": total > cap, "total": total, "now": t.isoformat(), **extra})
    body = '{"type":"FeatureCollection","features":[' + ",".join(feats) + "]," + head[1:]
    return Response(body, media_type="application/json")


def _emit(
    kind: str,
    rows: _Rows,
    t: datetime,
    hot_fn: Callable[[], set[str]],
    fmt: str,
    cap: int,
    extra: dict[str, Any],
) -> JSONResponse:
    """GeoJSON or columnar ("compact") response for `air` / `sea` rows, newest first."""
    total, hot = len(rows), hot_fn()
    picked = rows[:cap]
    age = [max(0.0, round((t - o.ts).total_seconds(), 1)) for o, _ in picked]
    head: dict[str, Any] = {
        "truncated": total > cap,
        "total": total,
        "now": t.isoformat(),
        **extra,
    }
    if kind == "air":
        os = [o for o, _ in picked]
        cols: dict[str, list[Any]] = {
            "hex": [o.hex for o in os],
            "flight": [(o.flight or "").strip() or None for o in os],
            "registration": [o.registration for o in os],
            "type_code": [o.type_code for o in os],
            "mil": [o.mil for o in os],
            "alt_baro": [o.alt_baro for o in os],
            "gs": [o.gs for o in os],
            "track": [o.track for o in os],
            "on_ground": [o.on_ground for o in os],
            "provider": [o.provider for o in os],
            "providers": [sorted(p) for _, p in picked],
            "age_s": age,
            "gps_bad": [gps_bad(o) for o in os],
            "in_alert": [o.hex.lower() in hot for o in os],
        }
    else:
        os = [o for o, _ in picked]
        cols = {
            "mmsi": [v.mmsi for v in os],
            "name": [v.name for v in os],
            "ship_type": [v.ship_type for v in os],
            "ship_class": [ship_class(v.ship_type) for v in os],
            "sog": [v.sog for v in os],
            "cog": [v.cog for v in os],
            "heading": [v.heading for v in os],
            "destination": [v.destination for v in os],
            "provider": [v.provider for v in os],
            "age_s": age,
            "in_alert": [str(v.mmsi) in hot for v in os],
        }
    cols["lon"] = [o.lon for o in os]
    cols["lat"] = [o.lat for o in os]
    if fmt == "compact":
        return _json(
            {
                "type": "compact",
                "kind": kind,
                "n": len(os),
                **head,
                "lat": [o.lat for o in os],
                "lon": [o.lon for o in os],
                "cols": cols,
            }
        )
    names = list(cols)
    feats = [_point(o.lat, o.lon, {k: cols[k][i] for k in names}) for i, o in enumerate(os)]
    return _json({"type": "FeatureCollection", "features": feats, **head})


def density(points: Iterable[tuple[float, float, bool]], zoom: float) -> dict[str, Any]:
    """H3 density cells (`density_res(zoom)`): count and military count per cell."""
    res = density_res(zoom)
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


def traffic_router(
    store: Store,
    now: Callable[[], datetime] | None = None,
    index: LivePositions | None = None,
) -> APIRouter:
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

    def _index_rows(
        layer: Layer,
        cap: int,
        bb: BBox,
        t: datetime,
        max_age_s: int,
        want: set[str] | None,
        since: int | None,
    ) -> tuple[list[Entry], dict[str, Any]]:
        """Entries from the live index plus the version/delta envelope fields."""
        extra: dict[str, Any] = {"version": layer.version}
        delta = since is not None and since <= layer.version
        removed = layer.removed_since(since) if delta and since is not None else None
        if delta and removed is not None and since is not None:
            extra.update({"delta": True, "since": since, "removed": removed})
            return layer.query(bb, t, max_age_s, want, since=since, limit=cap), extra
        extra["delta"] = False
        return layer.query(bb, t, max_age_s, want, limit=cap), extra

    dcache: dict[tuple[int, int, int], str] = {}  # serialised world density per version

    def _density_resp(
        layer: Layer,
        ents_fn: Callable[[], list[Entry]],
        bb: BBox,
        zoom: float,
        want: set[str] | None,
        max_age_s: int,
    ) -> Any:
        res = density_res(zoom)
        w, s, e, n = bb
        if (
            w <= -179.9
            and e >= 179.9
            and s <= -85
            and n >= 85
            and want is None
            and max_age_s >= layer.base_max_age_s
        ):
            ck = (id(layer), layer.version, res)
            body = dcache.get(ck)
            if body is None:
                cells = layer.density_world(res)
                head = {"type": "density", "res": res, "total": layer.total(), "cells": cells}
                body = json.dumps({**head, "version": layer.version, "delta": False})
                dcache.clear()
                dcache[ck] = body
            return Response(body, media_type="application/json")
        else:
            counts: dict[str, list[int]] = {}
            ents = ents_fn()
            for x in ents:
                c = counts.setdefault({3: x.r3, 4: x.r4, 5: x.r5}[res], [0, 0])
                c[0] += 1
                c[1] += int(x.mil)
            cells = [{"h3": k, "count": v[0], "mil": v[1]} for k, v in counts.items()]
            total = len(ents)
        return _json(
            {
                "type": "density",
                "res": res,
                "total": total,
                "cells": cells,
                "version": layer.version,
                "delta": False,
            }
        )

    @r.get("/api/coverage")
    def coverage(band: Band, bbox: str = "-180,-90,180,90") -> dict[str, Any]:
        """Receivers per H3 r4 cell over the last hour for `band` (ADR-008): aircraft reporting
        NIC/NACp for GNSS L1 / 1090 MHz, AIS ships for marine VHF. Live index only."""
        bb, t = parse_bbox(bbox), clock()
        out: dict[str, Any] = {"band": band.value, "window_s": COVERAGE_WINDOW_S, "cells": []}
        layer = None
        if index is not None:
            if band in (Band.L1_GNSS, Band.L_1090):
                layer = index.air
            elif band == Band.VHF_MARINE:
                layer = index.sea
        if layer is None:
            return {**out, "source": "none"}
        counts: dict[str, int] = {}
        for e in layer.query(bb, t, COVERAGE_WINDOW_S):
            if e.is_global:
                continue
            o = e.obs
            if isinstance(o, AircraftObs) and o.nic is None and o.nac_p is None:
                continue
            counts[e.r4] = counts.get(e.r4, 0) + 1
        out["cells"] = [{"h3": k, "receivers": v} for k, v in sorted(counts.items())]
        return {**out, "source": "live"}

    @r.get("/api/traffic/air")
    def air(
        bbox: str,
        max_age_s: int = Query(60, ge=1, le=86400),
        sources: str | None = None,
        zoom: float | None = Query(None, ge=0, le=24),
        since: int | None = Query(None, ge=0),
        fmt: str = Query("geojson", pattern="^(geojson|compact)$"),
    ) -> Any:
        bb, want, t = parse_bbox(bbox), _source_filter(sources), clock()
        cap = ZOOM_CAP if zoom is not None else CAP
        if index is not None:
            dens = zoom is not None and zoom < DENSITY_BELOW_ZOOM
            if dens and zoom is not None:
                return _density_resp(
                    index.air,
                    lambda: _index_rows(index.air, cap, bb, t, max_age_s, want, None)[0],
                    bb,
                    zoom,
                    want,
                    max_age_s,
                )
            ents, extra = _index_rows(index.air, cap, bb, t, max_age_s, want, since)
            if fmt == "geojson":
                return _emit_fast("air", ents, t, in_alert_entities, cap, extra)
            return _emit_compact_fast("air", ents, t, in_alert_entities, cap, extra)
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
        picked = sorted(
            (o for o in latest.values() if in_bbox(o.lat, o.lon, bb)),
            key=lambda x: x.ts,
            reverse=True,
        )
        if zoom is not None and zoom < DENSITY_BELOW_ZOOM:
            return _json(density(((o.lat, o.lon, o.mil) for o in picked), zoom))
        return _emit("air", [(o, provs[o.hex]) for o in picked], t, in_alert_entities, fmt, cap, {})

    @r.get("/api/traffic/sea")
    def sea(
        bbox: str,
        max_age_s: int = Query(900, ge=1, le=86400),
        sources: str | None = None,
        zoom: float | None = Query(None, ge=0, le=24),
        since: int | None = Query(None, ge=0),
        fmt: str = Query("geojson", pattern="^(geojson|compact)$"),
    ) -> Any:
        bb, want, t = parse_bbox(bbox), _source_filter(sources), clock()
        cap = ZOOM_CAP if zoom is not None else CAP
        if index is not None:
            dens = zoom is not None and zoom < DENSITY_BELOW_ZOOM
            if dens and zoom is not None:
                return _density_resp(
                    index.sea,
                    lambda: _index_rows(index.sea, cap, bb, t, max_age_s, want, None)[0],
                    bb,
                    zoom,
                    want,
                    max_age_s,
                )
            ents, extra = _index_rows(index.sea, cap, bb, t, max_age_s, want, since)
            if fmt == "geojson":
                return _emit_fast("sea", ents, t, in_alert_entities, cap, extra)
            return _emit_compact_fast("sea", ents, t, in_alert_entities, cap, extra)
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
        picked = sorted(
            (v for v in latest.values() if in_bbox(v.lat, v.lon, bb)),
            key=lambda x: x.ts,
            reverse=True,
        )
        if zoom is not None and zoom < DENSITY_BELOW_ZOOM:
            return _json(
                density(
                    ((v.lat, v.lon, ship_class(v.ship_type) == "military") for v in picked), zoom
                )
            )
        return _emit("sea", [(v, ()) for v in picked], t, in_alert_entities, fmt, cap, {})

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
