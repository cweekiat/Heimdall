"""Internet outage detector over IODA `outage_signal` and Cloudflare `outage_annotation` GeoEvents.

Assumptions (ADR-007, task 021b)
--------------------------------
* `outage_signal.value` is observed/baseline (IODA already normalises); when `value` is None it is
  recomputed from `attrs.observed / attrs.baseline` (skipped if baseline <= 0).
* An entity (`entity_type:entity_code`) is located at the mean of its events' lat/lon (the
  connector sets the entity centroid) and must lie within the AOI radius + `aoi_margin_km`
  (countries and regions are large, their centroid can sit outside a circular AOI).
* O1: in one time bin (`ts`) >= `min_datasources` datasources have ratio <= `drop_ratio_max`.
  Requiring the same bin makes a lone-datasource blip (e.g. a ping-only measurement glitch) silent.
* O2: an active annotation (`valid_from <= window_end`, `valid_to` None or >= window_start) whose
  `attrs.location` equals the entity code (case-insensitive). Supporting only, unless
  `annotation_alone` (then an annotation with lat/lon opens Evidence with no signal data).
* **Untrusted text**: `title`/`text` are never read; `attrs.location` is re-sanitised to
  `[A-Za-z0-9_-]` (32 chars) before it can enter an entity id.
* Persistence (>= 2 windows) is fusion's job; power-grid and ASN geolocation are out of scope.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from heimdall.detect.common import align_window, cfg_get, evidence_id
from heimdall.geo import KM_PER_NM, h3_cell, haversine_km
from heimdall.schemas import AlertKind, Coverage, Evidence, GeoEvent, RecordRef, RuleResult

NAME = "outage"
VERSION = "outage-1.0.0"

DEFAULTS: dict[str, Any] = {
    "window_h": 6,
    "drop_ratio_max": 0.7,
    "min_datasources": 2,
    "annotation_alone": False,
    "datasources": ["bgp", "ping-slash24", "merit-nt"],
    "aoi_margin_km": 100,
    "bin_min": 5,
    "step_min": 5,
    "h3_res": 4,
    "annotation_bonus": 0.15,
    "max_records": 50,
}
_BAD = re.compile(r"[^A-Za-z0-9_-]")


class AoiLike(Protocol):
    name: str
    lat: float
    lon: float
    radius_nm: float


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def _safe(x: Any) -> str:
    return _BAD.sub("", str(x or ""))[:32]


def _ratio(e: GeoEvent) -> float | None:
    if e.value is not None:
        return float(e.value)
    obs, base = e.attrs.get("observed"), e.attrs.get("baseline")
    if isinstance(obs, (int, float)) and isinstance(base, (int, float)) and base > 0:
        return float(obs) / float(base)
    return None


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def detect(
    events: Sequence[GeoEvent], cfg: dict[str, Any], now: datetime, *, aoi: AoiLike
) -> list[Evidence]:
    """One OUTAGE Evidence per entity with a multi-datasource drop in the window."""
    c = _cfg(cfg)
    start, end = align_window(now, c["window_h"] * 60, c["step_min"])
    lim_km = aoi.radius_nm * KM_PER_NM + c["aoi_margin_km"]
    thr = float(c["drop_ratio_max"])

    def near(e: GeoEvent) -> bool:
        return (
            e.lat is not None
            and e.lon is not None
            and haversine_km(e.lat, e.lon, aoi.lat, aoi.lon) <= lim_km
        )

    signals: dict[str, list[GeoEvent]] = defaultdict(list)
    for e in events:
        if e.kind != "outage_signal" or not (start <= e.ts < end) or not near(e):
            continue
        code = _safe(e.attrs.get("entity_code"))
        if code:
            signals[f"{_safe(e.attrs.get('entity_type')) or 'entity'}:{code}"].append(e)

    annos: dict[str, list[GeoEvent]] = defaultdict(list)
    for e in events:
        if e.kind != "outage_annotation" or not near(e):
            continue
        if (e.valid_from is not None and e.valid_from >= end) or (
            e.valid_to is not None and e.valid_to < start
        ):
            continue
        loc = _safe(e.attrs.get("location")).upper()
        if loc:
            annos[loc].append(e)

    entities: dict[str, tuple[list[GeoEvent], list[GeoEvent]]] = {}
    for key, sig in signals.items():
        entities[key] = (sig, annos.get(key.split(":", 1)[1].upper(), []))
    if c["annotation_alone"]:
        matched = {k.split(":", 1)[1].upper() for k in signals}
        for loc, an in annos.items():
            if loc not in matched:
                entities[f"annotation:{loc}"] = ([], an)

    configured = [str(d) for d in c["datasources"]]
    out: list[Evidence] = []
    for key, (sig, an) in sorted(entities.items()):
        by_bin: dict[datetime, dict[str, float]] = defaultdict(dict)
        with_data: set[str] = set()
        for e in sig:
            ds = _safe(e.attrs.get("datasource")) or "unknown"
            with_data.add(ds)
            r = _ratio(e)
            if r is not None:
                prev = by_bin[e.ts].get(ds)
                by_bin[e.ts][ds] = r if prev is None else min(prev, r)
        qual = {
            t: {d: r for d, r in m.items() if r <= thr}
            for t, m in by_bin.items()
            if sum(1 for r in m.values() if r <= thr) >= c["min_datasources"]
        }
        o1 = bool(qual)
        o2 = bool(an)
        if not (o1 or (c["annotation_alone"] and o2)):
            continue
        all_r = [r for m in by_bin.values() for r in m.values()]
        q_r = [r for m in qual.values() for r in m.values()]
        min_ratio = min(q_r) if q_r else (min(all_r) if all_r else float("nan"))
        n_ds = max((len(m) for m in qual.values()), default=0)
        if qual:
            ts_q = sorted(qual)
            duration_h = ((ts_q[-1] - ts_q[0]) + timedelta(minutes=c["bin_min"])).total_seconds()
            duration_h /= 3600
        else:
            duration_h = 0.0
        loc_src = sig or an
        lat = sum(float(e.lat or 0.0) for e in loc_src) / len(loc_src)
        lon = sum(float(e.lon or 0.0) for e in loc_src) / len(loc_src)
        cell = h3_cell(lat, lon, c["h3_res"])
        score = _clamp((1.0 - min_ratio) if o1 else 0.3)
        if o1 and o2:
            score = _clamp(score + c["annotation_bonus"])
        recs = [e for e in sig if e.ts in qual] + an
        recs = sorted(recs, key=lambda e: e.id)[: c["max_records"]]
        gaps = [f"datasource missing: {d}" for d in configured if d not in with_data]
        feats: dict[str, float] = {
            "n_datasources": float(n_ds),
            "duration_h": duration_h,
            "annotation_active": float(o2),
        }
        if min_ratio == min_ratio:  # not NaN
            feats["min_ratio"] = float(min_ratio)
        out.append(
            Evidence(
                id=evidence_id(AlertKind.OUTAGE.value, cell, f"ioda|{key}", start, aoi.name),
                kind=AlertKind.OUTAGE,
                detector=NAME,
                detector_version=VERSION,
                aoi=aoi.name,
                window_start=start,
                window_end=end,
                h3_cell=cell,
                h3_res=c["h3_res"],
                lat=lat,
                lon=lon,
                geometry=None,
                score=score,
                rules=[
                    RuleResult(
                        rule="O1_multi_datasource_drop",
                        description=(
                            f"Datasources below {thr:g} of baseline in the same time bin ({key})"
                        ),
                        value=float(n_ds),
                        threshold=float(c["min_datasources"]),
                        op=">=",
                        passed=o1,
                        baseline=1.0,
                    ),
                    RuleResult(
                        rule="O2_annotation_active",
                        description="Cloudflare Radar annotation active for the same location",
                        value=float(len(an)),
                        threshold=1.0,
                        op=">=",
                        passed=o2,
                    ),
                ],
                features=feats,
                records=[RecordRef(source=e.source, id=e.id, url=e.url) for e in recs],
                entities=[key],
                counterfactual=(
                    f"would not fire with < {c['min_datasources']} datasources at or below "
                    f"{thr:g} of baseline in the same bin"
                    + ("" if c["annotation_alone"] else " (an annotation alone is not enough)")
                ),
                coverage=Coverage(
                    n_observed=len(with_data),
                    n_quality=n_ds,
                    providers=sorted({e.source for e in sig + an}),
                    confidence=_clamp(len(with_data) / max(1, len(configured))),
                ),
                gaps=gaps,
                created_at=now,
            )
        )
    return out
