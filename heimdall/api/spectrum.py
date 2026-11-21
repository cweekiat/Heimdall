"""Spectrum status board API. Read-only; computed from stored band metrics.

GET /api/spectrum/status -> {generated, bands: [{band, label, unit, direction}],
    areas: [{aoi, label, bands: {<band>: {state, value, median, z, n, n_history, explanations,
    spark: [24 hourly means]}}}]}
State is recomputed from the metric history on each request (cached 30 s), so it survives restarts
and works for any Store; `stale_min` without a new value -> no_coverage.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter

from heimdall import config
from heimdall.detect import spectrum_state as ss
from heimdall.fuse.fusion import ACTIVE
from heimdall.geo import haversine_km
from heimdall.ports import Store
from heimdall.schemas import AlertKind

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


def ellipse_ring(
    lat: float, lon: float, major_km: float, minor_km: float, angle_deg: float, n: int = 36
) -> list[list[float]]:
    """Closed lon/lat ring of an ellipse (semi-axes in km, angle from north, clockwise)."""
    a, b, t = max(major_km, 0.5), max(minor_km, 0.5), math.radians(angle_deg)
    out = []
    for k in range(n + 1):
        u = 2 * math.pi * k / n
        x, y = a * math.cos(u), b * math.sin(u)  # x along the major axis
        north = x * math.cos(t) - y * math.sin(t)
        east = x * math.sin(t) + y * math.cos(t)
        out.append(
            [
                round(lon + east / (111.32 * max(0.05, math.cos(math.radians(lat)))), 5),
                round(lat + north / 110.57, 5),
            ]
        )
    return out


JAMMER_MERGE_KM = 150.0  # 041: estimates closer than this are one emitter seen from many cells
JAMMER_MAX_MAJOR_KM = 300.0  # 041: wider ellipses say "somewhere in the region", not a location


def merge_jammers(feats: list[dict[str, Any]], km: float = JAMMER_MERGE_KM) -> list[dict[str, Any]]:
    """One feature per emitter: best fit (highest agreement, then smallest ellipse) first, and
    every estimate within `km` of an already kept one folds into it (its alert refs are listed)."""
    ranked = sorted(
        feats,
        key=lambda f: (-(f["properties"].get("agreement") or 0.0), f["properties"]["major_km"]),
    )
    kept: list[dict[str, Any]] = []
    for f in ranked:
        p = f["properties"]
        home = next(
            (
                k
                for k in kept
                if haversine_km(p["lat"], p["lon"], k["properties"]["lat"], k["properties"]["lon"])
                <= km
            ),
            None,
        )
        if home is None:
            kept.append({**f, "properties": {**p, "alerts": [p["ref"]], "n_alerts": 1}})
        else:
            hp = home["properties"]
            hp["alerts"].append(p["ref"])
            hp["n_alerts"] += 1
            hp["last_seen"] = max(hp["last_seen"], p["last_seen"])
    return kept


def jammers(store: Store) -> dict[str, Any]:
    """Estimated jammer locations from open GNSS-jamming alerts (lead evidence per alert).

    041: only radio-horizon fits are shown (a centroid of degraded aircraft is the affected area,
    not an emitter), ellipses wider than JAMMER_MAX_MAJOR_KM are dropped, and nearby estimates of
    the same emitter are merged."""
    feats = []
    for al in store.list_alerts(status=list(ACTIVE), limit=500):
        if al.kind != AlertKind.GNSS_JAMMING:
            continue
        evs = [e for e in store.get_evidence(al.evidence_ids) if "emitter_lat" in e.features]
        if not evs:
            continue
        e = max(evs, key=lambda x: x.window_end)
        f = e.features
        if f.get("emitter_method") != 1.0 or f.get("ellipse_major_km", 0.0) > JAMMER_MAX_MAJOR_KM:
            continue
        lat, lon = f["emitter_lat"], f["emitter_lon"]
        ring = ellipse_ring(
            lat, lon, f.get("ellipse_major_km", 5), f.get("ellipse_minor_km", 5),
            f.get("ellipse_angle_deg", 0),
        )  # fmt: skip
        feats.append(
            {
                "type": "Feature",
                "id": al.id,
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "ref": al.ref,
                    "aoi": al.aoi,
                    "severity": al.severity.value,
                    "lat": lat,
                    "lon": lon,
                    "method": "horizon_fit" if f.get("emitter_method") == 1.0 else "centroid",
                    "agreement": f.get("emitter_agreement"),
                    "major_km": f.get("ellipse_major_km"),
                    "bad": f.get("bad"),
                    "total": f.get("total"),
                    "last_seen": al.last_seen.isoformat(),
                },
            }
        )
    return {"type": "FeatureCollection", "features": merge_jammers(feats)}


def space_weather(store: Store, now: datetime) -> dict[str, Any]:
    ev = store.events_window(
        now - timedelta(hours=24), now + timedelta(minutes=5), ["space_weather"]
    )
    if not ev:
        return {"available": False}
    latest = max(ev, key=lambda e: e.ts)
    a = latest.attrs
    kp_max = max(float(e.attrs.get("kp") or 0) for e in ev)
    return {
        "available": True,
        "ts": latest.ts.isoformat(),
        "kp": a.get("kp"),
        "kp_max_24h": kp_max,
        "g_scale": a.get("g_scale"),
        "r_scale": a.get("r_scale"),
        "s_scale": a.get("s_scale"),
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

    @r.get("/api/jammers")
    def get_jammers() -> dict[str, Any]:
        return jammers(st)

    @r.get("/api/spaceweather")
    def get_space_weather() -> dict[str, Any]:
        return space_weather(st, datetime.now(UTC))

    return r
