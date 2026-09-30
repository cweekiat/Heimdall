"""Spectrum status board API (task 033, ADR-008). Read-only; computed from stored band metrics.

GET /api/spectrum/status -> {generated, bands: [{band, label, unit, direction}],
    areas: [{aoi, label, bands: {<band>: {state, value, median, z, n, n_history, explanations,
    spark: [24 hourly means]}}}]}
State is recomputed from the metric history on each request (cached 30 s), so it survives restarts
and works for any Store; `stale_min` without a new value -> no_coverage.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter

from heimdall import config
from heimdall.detect import spectrum_state as ss
from heimdall.ports import Store

CACHE_S = 30.0
STALE_MIN = 20


def _spark(hist: list[tuple[datetime, float]], now: datetime) -> list[float | None]:
    """24 hourly means, oldest first (None = no value that hour)."""
    buckets: list[list[float]] = [[] for _ in range(24)]
    for ts, v in hist:
        age_h = int((now - ts).total_seconds() // 3600)
        if 0 <= age_h < 24:
            buckets[23 - age_h].append(v)
    return [round(sum(b) / len(b), 2) if b else None for b in buckets]


def band_status(store: Store, now: datetime) -> dict[str, Any]:
    s = config.settings()
    cfg = dict(s.detectors.get("spectrum_state", {}))
    days = float(cfg.get("baseline_days", ss.DEFAULTS["baseline_days"]))
    sw = store.events_window(now - timedelta(hours=6), now, ["space_weather"])
    areas = []
    for aoi in s.aois:
        cells: dict[str, Any] = {}
        for band in ss.INDICATORS:
            key = f"{aoi.name}|{band.value}"
            hist = store.metric_history(key, ss.METRIC, now - timedelta(days=days))
            ns = store.metric_history(key, ss.N_METRIC, now - timedelta(minutes=STALE_MIN))
            latest = (
                hist[-1] if hist and now - hist[-1][0] <= timedelta(minutes=STALE_MIN) else None
            )
            ind = ss.Indicator(band, latest[1] if latest else None, int(ns[-1][1]) if ns else 0)
            past = [(t, v) for t, v in hist if latest is None or t < latest[0]]
            st = ss.assess(
                aoi.name,
                ind,
                ss.history_for(band, past, now),
                cfg,
                ss.natural_explanations(band, sw, now),
            )
            cells[band.value] = {
                "state": st.state,
                "value": None if st.value is None else round(st.value, 2),
                "median": None if st.median is None else round(st.median, 2),
                "z": st.z,
                "n": st.n,
                "n_history": st.n_history,
                "explanations": st.explanations,
                "spark": _spark(hist, now),
            }
        areas.append({"aoi": aoi.name, "label": aoi.label or aoi.name, "bands": cells})
    return {
        "generated": now.isoformat(),
        "bands": [
            {"band": b.value, "label": sp["label"], "unit": sp["unit"], "direction": sp["dir"]}
            for b, sp in ss.INDICATORS.items()
        ],
        "areas": areas,
    }


def spectrum_router(st: Store) -> APIRouter:
    r = APIRouter()
    cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @r.get("/api/spectrum/status")
    def status() -> dict[str, Any]:
        hit = cache.get("s")
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        out = band_status(st, datetime.now(UTC))
        cache["s"] = (time.monotonic(), out)
        return out

    return r
