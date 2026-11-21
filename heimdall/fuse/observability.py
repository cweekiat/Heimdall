"""Observability context: "can this be checked, and when?".

Pure function returning `Corroboration(relation="context")` notes from `imagery_footprint`
(STAC catalog metadata) and `overpass` GeoEvents. Context kinds never count as corroborating
families and never create alerts.

Assumptions
-----------
* A footprint counts when its acquisition time `ts` lies in
  `[window_start - footprint_window_h, window_end + footprint_window_h]` and its GeoJSON
  Polygon/MultiPolygon contains the Evidence point (or any vertex of the Evidence geometry), or the
  Evidence polygon contains a footprint vertex. Planar tests in (lat, lon); fine for scene-sized
  areas away from the antimeridian. Footprints without geometry are skipped.
* Overpasses: kind `overpass`, start time = `valid_from` or `ts`, >= `now`; when both carry an AOI
  name they must match. The `n_next` earliest are reported.
* Only metadata is used. Assets, thumbnails, `text`, `title` and `url` are never read or echoed;
  notes are templates over numbers plus a fixed label from a whitelist (Sentinel-1/-2) or a
  strictly alphanumeric sensor code.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from heimdall.detect.common import cfg_get
from heimdall.geo import point_in_polygon
from heimdall.schemas import Corroboration, Domain, Evidence, GeoEvent

DEFAULTS: dict[str, Any] = {"footprint_window_h": 48, "n_next": 3, "max_footprints": 5}
_CODE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def _rings(geom: dict[str, Any] | None) -> list[list[tuple[float, float]]]:
    """Exterior rings of a GeoJSON Polygon/MultiPolygon as (lat, lon) vertex lists."""
    if not isinstance(geom, dict):
        return []
    coords = geom.get("coordinates")
    kind = geom.get("type")
    polys: list[Any]
    if kind == "Polygon":
        polys = [coords]
    elif kind == "MultiPolygon":
        polys = list(coords or [])
    else:
        return []
    out: list[list[tuple[float, float]]] = []
    for p in polys:
        try:
            ring = [(float(pt[1]), float(pt[0])) for pt in p[0]]
        except (TypeError, ValueError, IndexError):
            continue
        if len(ring) >= 3:
            out.append(ring)
    return out


def _intersects(ev: Evidence, fp: GeoEvent) -> bool:
    fp_rings = _rings(fp.geometry)
    if not fp_rings:
        return False
    ev_rings = _rings(ev.geometry)
    pts: list[tuple[float, float]] = []
    if ev.lat is not None and ev.lon is not None:
        pts.append((ev.lat, ev.lon))
    for r in ev_rings:
        pts.extend(r)
    if any(point_in_polygon(la, lo, ring) for la, lo in pts for ring in fp_rings):
        return True
    return any(
        point_in_polygon(la, lo, er) for er in ev_rings for ring in fp_rings for la, lo in ring
    )


def _platform(fp: GeoEvent) -> str:
    coll = f"{fp.attrs.get('collection', '')} {fp.attrs.get('platform', '')}".lower()
    if "sentinel-2" in coll or "sentinel2" in coll:
        return "Sentinel-2"
    if "sentinel-1" in coll or "sentinel1" in coll:
        return "Sentinel-1"
    return "Imagery"


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    f = float(x)
    return f if f == f and abs(f) != float("inf") else None


def observability(
    ev: Evidence, context: Sequence[GeoEvent], now: datetime, cfg: dict[str, Any]
) -> list[Corroboration]:
    """Recent footprints covering the Evidence, then the next `n_next` overpasses."""
    c = _cfg(cfg)
    w = timedelta(hours=float(c["footprint_window_h"]))
    lo, hi = ev.window_start - w, ev.window_end + w
    out: list[Corroboration] = []

    fps = sorted(
        (e for e in context if e.kind == "imagery_footprint" and lo <= e.ts <= hi),
        key=lambda e: (e.ts, e.id),
    )
    n_fp = 0
    for fp in fps:
        if n_fp >= int(c["max_footprints"]):
            break
        if not _intersects(ev, fp):
            continue
        n_fp += 1
        dh = (fp.ts - ev.window_end).total_seconds() / 3600.0
        when = f"{abs(dh):.0f} h {'before' if dh <= 0 else 'after'} the evidence window"
        cc = _num(fp.attrs.get("cloud_cover", fp.value)) if _platform(fp) == "Sentinel-2" else None
        cloud = f"; cloud cover {cc:.0f} %" if cc is not None else ""
        out.append(
            Corroboration(
                domain=Domain.SPACE,
                evidence_id=fp.id,
                relation="context",
                note=f"{_platform(fp)} scene acquired {when} covers this location{cloud}",
            )
        )

    def start(e: GeoEvent) -> datetime:
        return e.valid_from or e.ts

    passes = sorted(
        (
            e
            for e in context
            if e.kind == "overpass"
            and start(e) >= now
            and not (
                isinstance(e.attrs.get("aoi"), str) and ev.aoi and e.attrs.get("aoi") != ev.aoi
            )
        ),
        key=lambda e: (start(e), e.id),
    )
    for op in passes[: int(c["n_next"])]:
        dh = (start(op) - now).total_seconds() / 3600.0
        sensor = str(op.attrs.get("sensor", ""))
        label = sensor if _CODE.match(sensor) else "imaging satellite"
        el = _num(op.attrs.get("max_elev_deg", op.value))
        elev = f" (max elevation {el:.0f} deg)" if el is not None else ""
        out.append(
            Corroboration(
                domain=Domain.SPACE,
                evidence_id=op.id,
                relation="context",
                note=f"Next {label} overpass in {dh:.0f} h{elev}",
            )
        )
    return out
