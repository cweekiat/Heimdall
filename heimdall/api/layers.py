"""P3 map layers + alert context (task 023a, ADR-007). Read-only, GET only.

Only Store-port calls are used. Free text (`GeoEvent.text`) is never returned, and imagery events
expose the STAC item link (`url`) as data only: no asset/thumbnail hrefs, ever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query

from heimdall import config
from heimdall.config import AOI
from heimdall.ports import Store
from heimdall.schemas import GeoEvent

# layer -> event kinds served
LAYER_KINDS: dict[str, list[str]] = {
    "thermal": ["thermal"],
    "outage": ["outage_signal", "outage_annotation"],
    "night_lights": ["night_lights"],
    "imagery": ["imagery_footprint"],
    "overpass": ["overpass"],
}
# explicit per-kind attr whitelist (ADR-007 table); anything else is dropped
ATTRS: dict[str, tuple[str, ...]] = {
    "thermal": ("satellite", "instrument", "confidence", "daynight"),
    "outage_signal": ("entity_type", "entity_code", "datasource", "observed", "baseline", "level"),
    "outage_annotation": ("location", "asn", "scope", "outage_type"),
    "night_lights": (
        "h3_r4",
        "date",
        "product",
        "n_valid",
        "n_total",
        "valid_frac",
        "cloud_frac",
        "median",
        "p90",
    ),
    "imagery_footprint": (
        "collection",
        "platform",
        "stac_item_id",
        "orbit_state",
        "relative_orbit",
        "cloud_cover",
    ),
    "overpass": ("norad_id", "sat_name", "sensor", "aoi", "max_elev_deg"),
}
_SCALAR = (str, int, float, bool, type(None))
NEXT_H = 48
MAX_OVERPASSES = 3
AID = Path(pattern=r"^[A-Za-z0-9_.:|-]{1,80}$")


def _aoi_or_404(name: str) -> AOI:
    try:
        return config.settings().aoi(name)
    except StopIteration:
        raise HTTPException(404, "unknown aoi") from None


def _point(e: GeoEvent) -> tuple[float, float] | None:
    if e.lat is not None and e.lon is not None:
        return e.lat, e.lon
    g = e.geometry or {}
    try:
        if g.get("type") == "Polygon":
            ring = g["coordinates"][0]
            return (
                sum(p[1] for p in ring) / len(ring),
                sum(p[0] for p in ring) / len(ring),
            )
        if g.get("type") == "Point":
            return float(g["coordinates"][1]), float(g["coordinates"][0])
    except (KeyError, IndexError, TypeError):
        return None
    return None


def _in_aoi(e: GeoEvent, aoi: AOI) -> bool:
    if e.attrs.get("aoi") == aoi.name:
        return True
    p = _point(e)
    return p is not None and aoi.contains(*p)


def _in_aoi_name(e: GeoEvent, name: str) -> bool:
    try:
        return _in_aoi(e, config.settings().aoi(name))
    except StopIteration:
        return False


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


def _feature(e: GeoEvent, layer: str) -> dict[str, Any] | None:
    props: dict[str, Any] = {
        "kind": e.kind,
        "source": e.source,
        "ts": _iso(e.ts),
        "valid_from": _iso(e.valid_from),
        "valid_to": _iso(e.valid_to),
        "value": e.value,
    }
    for k in ATTRS.get(e.kind, ()):
        v = e.attrs.get(k)
        if isinstance(v, _SCALAR):
            props[k] = v
    if layer == "imagery" and e.url:
        props["url"] = e.url  # STAC item link, text only (ADR-007 §3)
    geom = e.geometry
    if geom is None:
        p = _point(e)
        if p is None:
            return None
        geom = {"type": "Point", "coordinates": [p[1], p[0]]}
    return {"type": "Feature", "id": e.id, "geometry": geom, "properties": props}


def layers_router(st: Store) -> APIRouter:
    r = APIRouter()

    @r.get("/api/layers/{layer}")
    def get_layer(
        layer: str,
        aoi: str = Query(max_length=60),
        hours: int | None = Query(default=None),
    ) -> dict[str, Any]:
        if layer not in LAYER_KINDS:
            raise HTTPException(404, "unknown layer")
        cfg = config.settings().api.layers
        h = hours if hours is not None else (168 if layer == "night_lights" else 24)
        if not 1 <= h <= cfg.max_hours:
            raise HTTPException(422, f"hours must be 1..{cfg.max_hours}")
        area = _aoi_or_404(aoi)
        now = datetime.now(UTC)
        if layer == "overpass":  # forward-looking
            start, end = now, now + timedelta(hours=h)
        else:
            start, end = now - timedelta(hours=h), now + timedelta(minutes=5)
        rows = [e for e in st.events_window(start, end, LAYER_KINDS[layer]) if _in_aoi(e, area)]
        rows.sort(key=lambda e: e.ts, reverse=(layer != "overpass"))
        feats = [f for e in rows if (f := _feature(e, layer)) is not None]
        return {
            "type": "FeatureCollection",
            "features": feats[: cfg.max_features],
            "truncated": len(feats) > cfg.max_features,
            "layer": layer,
            "aoi": aoi,
            "hours": h,
        }

    @r.get("/api/alerts/{alert_id}/context")
    def alert_context(alert_id: str = AID) -> dict[str, Any]:
        al = st.get_alert(alert_id)
        if al is None:
            raise HTTPException(404, "unknown alert")
        confounders = []
        for c in al.corroborations:
            if c.relation != "context" or c.factor is None:
                continue  # footprint/overpass/co-location notes are not confounders
            confounders.append({"note": c.note, "factor": c.factor})
        gaps: list[str] = list(al.gaps)
        for ev in st.get_evidence(al.evidence_ids):
            gaps += [g for g in ev.gaps if g not in gaps]
        obs: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        area = al.aoi
        span = timedelta(hours=NEXT_H)
        past = st.events_window(now - span, now + timedelta(minutes=5), ["imagery_footprint"])
        for e in sorted(past, key=lambda x: x.ts, reverse=True):
            if area and not _in_aoi_name(e, area):
                continue
            obs.append(
                {
                    "note": f"Imagery footprint {e.attrs.get('stac_item_id', e.id)}",
                    "platform": e.attrs.get("platform"),
                    "sensor": e.attrs.get("collection"),
                    "time": _iso(e.valid_from or e.ts),
                    "kind": "footprint",
                    "id": e.id,
                    "cloud_cover": e.attrs.get("cloud_cover"),
                    "url": e.url,
                }
            )
        nxt = [
            e
            for e in st.events_window(now, now + span, ["overpass"])
            if area is None or e.attrs.get("aoi") == area
        ]
        nxt.sort(key=lambda x: x.valid_from or x.ts)
        for e in nxt[:MAX_OVERPASSES]:
            obs.append(
                {
                    "note": f"Next pass of {e.attrs.get('sat_name', 'satellite')}",
                    "platform": e.attrs.get("sat_name"),
                    "sensor": e.attrs.get("sensor"),
                    "time": _iso(e.valid_from or e.ts),
                    "kind": "overpass",
                    "id": e.id,
                    "cloud_cover": None,
                    "url": None,
                }
            )
        if not any(o["kind"] == "footprint" for o in obs):
            gaps.append(f"No imagery footprint in the last {NEXT_H} h")
        return {"confounders": confounders, "observability": obs, "gaps": gaps}

    return r
