"""P3 map layers + alert context (task 023a, ADR-007). Read-only, GET only.

Only Store-port calls are used. Free text (`GeoEvent.text`) is never returned, and imagery events
expose the STAC item link (`url`) as data only: no asset/thumbnail hrefs, ever.
"""

from __future__ import annotations

import time
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
    "imagery": ["imagery_footprint"],
    "overpass": ["overpass"],
    # task 030: spectrum layers (ADR-008). Regional (Indo-Pacific box), not per area.
    "hf": ["hf_spots"],
    "sar_rfi": ["sar_rfi"],
    "satnogs": ["sat_observation"],
    "smap": ["l_band_rfi"],
    "cygnss": ["l1_noise"],  # task 038: CYGNSS L1 noise floor per r3 cell-day
    "conflict": ["conflict_event"],  # task 049: GDELT conflict events (context, machine-coded news)
}
REGIONAL = frozenset(
    {"hf", "sar_rfi", "satnogs", "smap", "cygnss", "conflict"}
)  # aoi=all returns every feature
# explicit per-kind attr whitelist (ADR-007 table); anything else is dropped
ATTRS: dict[str, tuple[str, ...]] = {
    "thermal": ("satellite", "instrument", "confidence", "daynight"),
    "imagery_footprint": (
        "collection",
        "platform",
        "stac_item_id",
        "orbit_state",
        "relative_orbit",
        "cloud_cover",
    ),
    "overpass": ("norad_id", "sat_name", "sensor", "aoi", "max_elev_deg"),
    "hf_spots": ("band_mhz", "mode", "n", "paths", "n_rx", "n_tx", "snr_med", "mean_dist_km"),
    "sar_rfi": (
        "platform",
        "orbit_state",
        "relative_orbit",
        "rfi_detected",
        "max_power_ratio",
        "stac_item_id",
    ),
    "sat_observation": (
        "status",
        "norad_cat_id",
        "sat_name",
        "rf_band",
        "frequency_hz",
        "mode",
        "max_altitude_deg",
        "station_id",
    ),
    "l1_noise": (
        "day",
        "h3",
        "n",
        "n_sats",
        "excess_db",
        "ocean_frac",
        "rfi_frac",
        "base_db",
        "base_mad_db",
        "base_days",
    ),
    "l_band_rfi": ("h3", "n_footprints", "n_rfi_h", "n_rfi_v", "rfi_frac", "tb_h_mean_k"),
    "conflict_event": (
        "cameo",
        "root",
        "label",
        "goldstein",
        "mentions",
        "sources",
        "articles",
        "place",
        "country",
        "precision",
        "event_day",
        "domain",
    ),
}
_SCALAR = (str, int, float, bool, type(None))
NEXT_H = 48
CACHE_S = 5.0  # per-tick response cache (layers change on pipeline ticks, minutes apart)
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
    if layer in ("imagery", "conflict") and e.url:
        props["url"] = e.url  # link as text only (ADR-007 §3); news pages are never fetched
    geom = e.geometry
    if geom is None:
        p = _point(e)
        if p is None:
            return None
        geom = {"type": "Point", "coordinates": [p[1], p[0]]}
    return {"type": "Feature", "id": e.id, "geometry": geom, "properties": props}


def layers_router(st: Store) -> APIRouter:
    r = APIRouter()
    cache: dict[tuple[str, int, str], tuple[float, dict[str, Any]]] = {}

    @r.get("/api/news")
    def news(limit: int = Query(40, ge=1, le=100)) -> dict[str, Any]:
        """049: latest headlines (BBC World, Al Jazeera). Untrusted text, display only: the UI
        writes it as text, nothing in Heimdall parses or acts on it."""
        now = datetime.now(UTC)
        evs = st.events_window(now - timedelta(hours=48), now + timedelta(minutes=5), ["news_item"])
        seen: set[str] = set()
        items = []
        for e in sorted(evs, key=lambda e: e.ts, reverse=True):
            key = (e.title or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "title": e.title,
                    "feed": e.attrs.get("feed"),
                    "url": e.url,
                    "ts": _iso(e.ts),
                    "untrusted": True,
                }
            )
        return {"items": items[:limit], "total": len(items)}

    @r.get("/api/layers/{layer}")
    def get_layer(
        layer: str,
        aoi: str = Query(max_length=60),
        hours: int | None = Query(default=None),
    ) -> Any:
        if layer not in LAYER_KINDS:
            raise HTTPException(404, "unknown layer")
        cfg = config.settings().api.layers
        h = hours if hours is not None else 24
        if not 1 <= h <= cfg.max_hours:
            raise HTTPException(422, f"hours must be 1..{cfg.max_hours}")
        every = aoi == "all"  # 026a: one request for all AOIs; features carry properties.aoi
        area = None if every else _aoi_or_404(aoi)
        now = datetime.now(UTC)
        ck = (layer, h, aoi)
        hit = cache.get(ck)
        if hit is not None and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        if layer == "overpass":  # forward-looking
            start, end = now, now + timedelta(hours=h)
        else:
            start, end = now - timedelta(hours=h), now + timedelta(minutes=5)
        events = st.events_window(start, end, LAYER_KINDS[layer])
        areas = config.settings().aois
        rows: list[tuple[GeoEvent, str]] = []
        for e in events:
            if every and layer in REGIONAL:
                rows.append((e, ""))
            elif area is not None:
                if _in_aoi(e, area):
                    rows.append((e, aoi))
            else:
                hit_aoi = next((a.name for a in areas if _in_aoi(e, a)), None)
                if hit_aoi is not None:
                    rows.append((e, hit_aoi))
        rows.sort(key=lambda r: r[0].ts, reverse=(layer != "overpass"))
        feats = []
        for e, name in rows:
            f = _feature(e, layer)
            if f is not None:
                f["properties"]["aoi"] = name
                feats.append(f)
        out = {
            "type": "FeatureCollection",
            "features": feats[: cfg.max_features],
            "truncated": len(feats) > cfg.max_features,
            "layer": layer,
            "aoi": aoi,
            "hours": h,
            "total": len(feats),
        }
        cache[ck] = (time.monotonic(), out)
        return out

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
