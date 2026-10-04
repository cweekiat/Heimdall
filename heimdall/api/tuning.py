"""Detector tuning API (task 061). Read-only: proposals are shown, never applied.

GET /api/tuning?days=30 -> heimdall.tuning.report (precision per detector, false-alarm reasons,
threshold proposals with the YAML line to change).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query

from heimdall import config, tuning
from heimdall.ports import Store

CACHE_S = 60.0


def tuning_router(st: Store) -> APIRouter:
    r = APIRouter()
    cache: dict[int, tuple[float, dict[str, Any]]] = {}

    @r.get("/api/tuning")
    def get_tuning(days: int = Query(30, ge=1, le=90)) -> dict[str, Any]:
        hit = cache.get(days)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        out = tuning.build(st, config.settings().detectors, datetime.now(UTC), days)
        cache[days] = (time.monotonic(), out)
        return out

    return r
