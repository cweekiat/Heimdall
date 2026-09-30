"""GNSS interference map API (task 031d). Descriptive context; never creates alerts.

GET /api/gnss/days  -> {"days": [YYYY-MM-DD...], "last24h": true}
GET /api/gnss/day?date=YYYY-MM-DD|last24h[&bbox=w,s,e,n]
    -> {date, incomplete, hours_with_data, sources, total_cells, method,
        cells: [{h3, good, bad, total, pct, cls}]}   (404 when that day has no map)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from heimdall.gnss_map import in_bbox
from heimdall.gnss_service import LAST24H, GnssService, valid_key

METHOD = (
    "GPSJam: pct = 100 * max(0, bad - 1) / total per H3 r4 cell; green <2, yellow 2-10, red >10"
)


def _bbox(v: str | None) -> tuple[float, float, float, float] | None:
    if not v:
        return None
    try:
        w, s, e, n = (float(x) for x in v.split(","))
    except ValueError as err:
        raise HTTPException(422, "bbox must be w,s,e,n") from err
    if not (-90 <= s <= n <= 90 and -180 <= w <= 180 and -180 <= e <= 180):
        raise HTTPException(422, "bbox out of range")
    return w, s, e, n


def gnss_router(svc: GnssService | None) -> APIRouter:
    r = APIRouter()

    @r.get("/api/gnss/days")
    def days() -> dict[str, Any]:
        return {"days": svc.days() if svc else [], "last24h": svc is not None}

    @r.get("/api/gnss/day")
    def day(date: str = Query(LAST24H), bbox: str | None = None) -> dict[str, Any]:
        if not valid_key(date):
            raise HTTPException(422, "date must be YYYY-MM-DD or last24h")
        d = svc.day(date) if svc else None
        if d is None:
            raise HTTPException(404, "no GNSS map for that date")
        box = _bbox(bbox)
        cells = in_bbox(d.cells, box) if box else d.cells
        return {
            "date": d.date,
            "incomplete": d.incomplete,
            "hours_with_data": d.hours_with_data,
            "sources": d.sources,
            "total_cells": len(d.cells),
            "method": METHOD,
            "cells": [
                {
                    "h3": c.h3,
                    "good": c.good,
                    "bad": c.bad,
                    "total": c.total,
                    "pct": c.pct,
                    "cls": c.cls,
                }
                for c in cells
            ],
        }

    return r
