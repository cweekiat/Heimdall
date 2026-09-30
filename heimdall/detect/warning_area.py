"""Active area notices (NAVWARN / NOTAM) as context Evidence (ADR-005 sections A and B).

Assumptions
-----------
* One `WARNING_AREA` Evidence per *active* notice of a configured category that intersects the AOI.
  Active = kind in {nav_warning, notam}, `attrs.status == "active"` (a cancelled notice is never
  active), `valid_from <= now` and (`valid_to` is None or `valid_to > now`; a notice without
  `valid_to` is treated as open-ended: NGA warnings are cancelled explicitly).
* AOI intersection: any covering cell (`attrs.h3_r4_cells`) or the centroid within
  `radius_nm` + `aoi_margin_km` (one r4 cell radius, 25 km) of the AOI centre. Notices with
  `parse_status` in {no_geometry, too_large} intersect by centroid only (partial location) and get
  reduced confidence.
* **Untrusted text**: `title` and `text` are never read. Only `attrs.ref` (re-sanitised to
  `[A-Z0-9/ -]`, 40 chars), `source`, `category`, `id`, `url` and times enter the Evidence.
* Context-only: fusion never opens an alert from this kind.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from heimdall.detect.common import align_window, cfg_get, evidence_id
from heimdall.geo import KM_PER_NM, h3_cell, h3_center, haversine_km
from heimdall.schemas import (
    AlertKind,
    Band,
    Coverage,
    Evidence,
    GeoEvent,
    RecordRef,
    RuleResult,
)

NAME = "warning_area"
VERSION = "warning-area-1.1.0"  # 029d: GNSS interference notices carry band L1_GNSS

NOTICE_KINDS = ("nav_warning", "notam")
DEFAULTS: dict[str, Any] = {
    "h3_res": 4,
    "step_min": 5,
    "categories": ["military_exercise", "live_firing", "missile_rocket", "gnss_interference"],
    "aoi_margin_km": 25,
    "partial_confidence": 0.6,
}
_REF_BAD = re.compile(r"[^A-Z0-9/ -]")


class AoiLike(Protocol):
    name: str
    lat: float
    lon: float
    radius_nm: float


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


_GNSS_RE = re.compile(
    r"(?<![A-Za-z])(?:GNSS|GPS|NAVSTAR|GLONASS|BEIDOU|GALILEO)(?![A-Za-z])", re.IGNORECASE
)


def notice_bands(e: GeoEvent) -> list[Band]:
    """L1_GNSS when the notice names GNSS/GPS (regex only; text is untrusted), else NONE."""
    hay = " ".join(str(x) for x in (e.attrs.get("category"), e.title, e.text) if x)
    return [Band.L1_GNSS] if _GNSS_RE.search(hay) else [Band.NONE]


def safe_ref(e: GeoEvent) -> str:
    """Sanitised short reference; falls back to the event id (also sanitised)."""
    raw = str(e.attrs.get("ref") or e.id).upper()
    return _REF_BAD.sub("", raw)[:40].strip()


def is_active(e: GeoEvent, now: datetime) -> bool:
    if e.kind not in NOTICE_KINDS or e.attrs.get("status", "active") != "active":
        return False
    if e.valid_from is not None and e.valid_from > now:
        return False
    return e.valid_to is None or e.valid_to > now


def covering_cells(e: GeoEvent) -> list[str]:
    cells = e.attrs.get("h3_r4_cells")
    return [str(x) for x in cells] if isinstance(cells, list) else []


def intersects_aoi(e: GeoEvent, aoi: AoiLike, margin_km: float) -> bool:
    lim = aoi.radius_nm * KM_PER_NM + margin_km
    if (
        e.lat is not None
        and e.lon is not None
        and haversine_km(e.lat, e.lon, aoi.lat, aoi.lon) <= lim
    ):
        return True
    for cell in covering_cells(e):
        la, lo = h3_center(cell)
        if haversine_km(la, lo, aoi.lat, aoi.lon) <= lim:
            return True
    return False


def detect(
    events: Sequence[GeoEvent], cfg: dict[str, Any], now: datetime, *, aoi: AoiLike
) -> list[Evidence]:
    """One WARNING_AREA Evidence per active configured-category notice intersecting `aoi`."""
    c = _cfg(cfg)
    start, end = align_window(now, 30, c["step_min"])
    cats = set(c["categories"])
    out: list[Evidence] = []
    for e in sorted(events, key=lambda x: x.id):
        cat = str(e.attrs.get("category") or "other")
        if (
            not is_active(e, now)
            or cat not in cats
            or not intersects_aoi(e, aoi, c["aoi_margin_km"])
        ):
            continue
        lat, lon = e.lat, e.lon
        cells = covering_cells(e)
        if (lat is None or lon is None) and cells:
            lat, lon = h3_center(cells[0])
        cell = e.h3_r4 or (
            h3_cell(lat, lon, c["h3_res"]) if lat is not None and lon is not None else ""
        )
        status = str(e.attrs.get("parse_status") or "ok")
        partial = status in ("no_geometry", "too_large", "partial")
        feats: dict[str, float] = {"n_cells": float(len(cells)), "partial_geometry": float(partial)}
        for k in ("alt_lower_ft", "alt_upper_ft"):
            if isinstance(e.attrs.get(k), (int, float)):
                feats[k] = float(e.attrs[k])
        if e.valid_from is not None:
            feats["valid_from_ts"] = e.valid_from.timestamp()
        if e.valid_to is not None:
            feats["valid_to_ts"] = e.valid_to.timestamp()
        ref = safe_ref(e)
        out.append(
            Evidence(
                id=evidence_id(AlertKind.WARNING_AREA.value, cell, e.id, start, aoi.name),
                kind=AlertKind.WARNING_AREA,
                detector=NAME,
                bands=notice_bands(e),
                detector_version=VERSION,
                aoi=aoi.name,
                window_start=start,
                window_end=end,
                h3_cell=cell or None,
                h3_res=c["h3_res"],
                lat=lat,
                lon=lon,
                geometry=e.geometry,
                score=0.5,
                rules=[
                    RuleResult(
                        rule="notice_active",
                        description=f"Notice {ref} ({e.source}) is active at evaluation time",
                        value=1.0,
                        threshold=1.0,
                        op="==",
                        passed=True,
                    ),
                    RuleResult(
                        rule="notice_category",
                        description=f"Category {cat} is a configured context category",
                        value=1.0,
                        threshold=1.0,
                        op="==",
                        passed=True,
                    ),
                    RuleResult(
                        rule="intersects_aoi",
                        description=f"Notice area intersects AOI {aoi.name}",
                        value=1.0,
                        threshold=1.0,
                        op="==",
                        passed=True,
                    ),
                ],
                features=feats,
                records=[RecordRef(source=e.source, id=e.id, url=e.url)],
                entities=[ref] if ref else [],
                counterfactual=(
                    "Context only: not an anomaly. Would not be listed if the notice were "
                    "cancelled, expired or outside the AOI."
                ),
                coverage=Coverage(
                    n_observed=1,
                    n_quality=0 if partial else 1,
                    providers=[e.source],
                    confidence=c["partial_confidence"] if partial else 1.0,
                ),
                gaps=["approximate location (centroid only)"] if partial else [],
                created_at=now,
            )
        )
    return out
