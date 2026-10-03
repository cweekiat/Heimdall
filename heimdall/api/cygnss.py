"""CYGNSS L1 noise map API (task 059). Descriptive context; the detector raises the alerts.

GET /api/cygnss/days            -> {"days": [YYYY-MM-DD, ... newest last]}
GET /api/cygnss/day?date=D      -> {date, total_cells, shown_cells, thresholds, method, limits,
                                    cells: [{h3, excess_db, delta_db, z, n, n_sats, ocean_frac,
                                             base_db, base_days, flag}]}
`date` defaults to the newest day. Only cells worth a look are sent (excess or rise over the cell's
own baseline >= MIN_SHOW_DB; over land and coasts the level floor is MIN_SHOW_LAND_DB; or flagged);
quiet cells are omitted, so an empty map means "nothing above the floor", not "no coverage".
`flag` = "level" (C1), "change" (C2) or null, from the detector's own `assess` and live thresholds.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from heimdall import config
from heimdall.detect import cygnss
from heimdall.detect.common import cfg_get
from heimdall.ports import Store

MIN_SHOW_DB = 1.0  # UI floor, not a threshold: open-ocean 99th pct ~1.5 dB (2026-09-27)
MIN_SHOW_LAND_DB = 2.0  # warm land / coast reads 1-2 dB high by nature; show the rise instead
DAYS_BACK = 30
CACHE_S = 300.0
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
METHOD = (
    "Median CYGNSS DDM noise floor per H3 r3 cell and UTC day, in dB above the satellite "
    "channel's open-ocean median; rise = vs the cell's own trailing median"
)
LIMITS = (
    "CYGNSS sees only 38 S to 38 N, with 2-3 days delay; a cell is tens to hundreds of km. Warm "
    "land and coasts read higher than open ocean. A quiet cell does not prove there is no jamming."
)


def _r(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(v, n)


def cygnss_router(st: Store) -> APIRouter:
    r = APIRouter()
    cache: dict[str, tuple[float, Any]] = {}

    def _events(lo: datetime, hi: datetime) -> list[Any]:
        return list(st.events_window(lo, hi, [cygnss.KIND]))

    def _days() -> list[str]:
        hit = cache.get("days")
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return list(hit[1])
        now = datetime.now(UTC)
        days = sorted(
            {e.ts.date().isoformat() for e in _events(now - timedelta(days=DAYS_BACK), now)}
        )
        cache["days"] = (time.monotonic(), days)
        return days

    @r.get("/api/cygnss/days")
    def days() -> dict[str, Any]:
        return {"days": _days()}

    @r.get("/api/cygnss/day")
    def day(date: str | None = Query(None, max_length=10)) -> dict[str, Any]:
        if date is not None and not DAY_RE.match(date):
            raise HTTPException(422, "date must be YYYY-MM-DD")
        all_days = _days()
        key = date or (all_days[-1] if all_days else "")
        if not key or key not in all_days:
            raise HTTPException(404, "no CYGNSS data for that date")
        hit = cache.get(f"day:{key}")
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return dict(hit[1])
        d0 = datetime.fromisoformat(key).replace(tzinfo=UTC)
        dcfg = dict(config.settings().detectors.get("cygnss", {}))
        c = {k: cfg_get(dcfg, k, v) for k, v in cygnss.DEFAULTS.items()}
        evs = [e for e in _events(d0, d0 + timedelta(days=1)) if e.ts.date() == d0.date()]
        cells = []
        for e in evs:
            a = cygnss.assess(e, c)
            n = int(e.attrs.get("n") or 0)
            flag = None
            if n >= c["min_samples"]:
                flag = "level" if a.c1 else "change" if a.c2 else None
            ocean = float(e.attrs.get("ocean_frac") or 0.0) >= c["ocean_frac_min"]
            floor = MIN_SHOW_DB if ocean else MIN_SHOW_LAND_DB
            if flag is None and a.excess < floor and (a.delta or 0.0) < MIN_SHOW_DB:
                continue
            cells.append(
                {
                    "h3": str(e.attrs.get("h3") or ""),
                    "excess_db": _r(a.excess),
                    "delta_db": _r(a.delta),
                    "z": _r(a.z, 1),
                    "n": n,
                    "n_sats": e.attrs.get("n_sats"),
                    "ocean_frac": e.attrs.get("ocean_frac"),
                    "base_db": e.attrs.get("base_db"),
                    "base_days": e.attrs.get("base_days"),
                    "flag": flag,
                }
            )
        out = {
            "date": key,
            "total_cells": len(evs),
            "shown_cells": len(cells),
            "min_show_db": MIN_SHOW_DB,
            "min_show_land_db": MIN_SHOW_LAND_DB,
            "thresholds": {
                "level_db": c["level_db"],
                "land_level_db": c["land_level_db"],
                "ocean_frac_min": c["ocean_frac_min"],
                "change_db": c["change_db"],
                "z_min": c["z_min"],
                "min_samples": c["min_samples"],
            },
            "method": METHOD,
            "limits": LIMITS,
            "cells": [x for x in cells if x["h3"]],
        }
        cache[f"day:{key}"] = (time.monotonic(), out)
        return out

    return r
