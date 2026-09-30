"""Thermal anomaly detector over FIRMS `thermal` GeoEvents (ADR-007, task 021a).

Assumptions
-----------
* Input events are `kind == "thermal"` (`value` = FRP in MW, `attrs.confidence` in
  low/nominal/high, `attrs.satellite`). Only `min_confidence` classes are used; `text`/`title`
  are never read (untrusted).
* Static-heat mask: an r8 cell (~0.7 km^2, about one VIIRS pixel) with detections on >=
  `static_days` distinct UTC days of the `baseline_days` before the window is a flare / industry /
  volcano and is dropped (all confidence classes count for the mask). Pixel jitter across r8
  boundaries can leak a flare; the ratio test (T3) is the second line of defence.
* Baseline for T3 is the mean detections per day over the r4 cell in the baseline period
  (static-masked events excluded). History length is estimated from the earliest baseline event
  of the whole feed; a quiet AOI with an empty baseline therefore reads as "short baseline".
* Persistence (>= 2 windows) is fusion's job. Land cover is not modelled (agricultural burning is
  handled only through the baseline ratio).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from heimdall.detect.common import align_window, cfg_get, evidence_id
from heimdall.geo import KM_PER_NM, h3_cell, haversine_km
from heimdall.schemas import AlertKind, Band, Coverage, Evidence, GeoEvent, RecordRef, RuleResult

NAME = "thermal"
VERSION = "thermal-1.0.0"

DEFAULTS: dict[str, Any] = {
    "window_h": 24,
    "min_confidence": ["nominal", "high"],
    "min_detections": 3,
    "frp_min_mw": 50.0,
    "baseline_days": 60,
    "static_days": 10,
    "ratio_min": 3.0,
    "expected_passes": 4,
    "step_min": 60,
    "pass_bin_min": 10,
    "short_baseline_cap": 0.6,
    "max_records": 50,
}


class AoiLike(Protocol):
    name: str
    lat: float
    lon: float
    radius_nm: float


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _pass_key(e: GeoEvent, bin_min: int) -> tuple[str, int]:
    sat = str(e.attrs.get("satellite") or e.source)
    return sat, int(e.ts.timestamp() // (bin_min * 60))


def detect(
    events: Sequence[GeoEvent],
    baseline_events: Sequence[GeoEvent],
    cfg: dict[str, Any],
    now: datetime,
    *,
    aoi: AoiLike,
) -> list[Evidence]:
    """One THERMAL Evidence per r6 fire cluster that is new relative to its r4 baseline."""
    c = _cfg(cfg)
    start, end = align_window(now, c["window_h"] * 60, c["step_min"])
    base_from = start - timedelta(days=c["baseline_days"])
    lim_km = aoi.radius_nm * KM_PER_NM
    confs = {str(x) for x in c["min_confidence"]}

    def located(e: GeoEvent) -> bool:
        return (
            e.kind == "thermal"
            and e.lat is not None
            and e.lon is not None
            and haversine_km(e.lat, e.lon, aoi.lat, aoi.lon) <= lim_km
        )

    base = [e for e in baseline_events if located(e) and base_from <= e.ts < start]
    # 1. static mask: r8 cells hot on >= static_days distinct days
    days: dict[str, set[Any]] = defaultdict(set)
    for e in base:
        assert e.lat is not None and e.lon is not None
        days[h3_cell(e.lat, e.lon, 8)].add(e.ts.date())
    static = {cell for cell, d in days.items() if len(d) >= c["static_days"]}

    def is_static(e: GeoEvent) -> bool:
        assert e.lat is not None and e.lon is not None
        return h3_cell(e.lat, e.lon, 8) in static

    # window events (confidence + AOI filtered); static ones are masked, counted per r4
    win_all = [
        e
        for e in events
        if located(e) and start <= e.ts < end and str(e.attrs.get("confidence")) in confs
    ]
    masked_r4: dict[str, int] = defaultdict(int)
    win: list[GeoEvent] = []
    for e in win_all:
        assert e.lat is not None and e.lon is not None
        if is_static(e):
            masked_r4[h3_cell(e.lat, e.lon, 4)] += 1
        else:
            win.append(e)

    # baseline per r4: static-masked, confidence-filtered detections per day
    hist_days = 0.0
    if baseline_events:
        first = min((e.ts for e in baseline_events if e.ts < start), default=None)
        hist_days = (start - first).total_seconds() / 86400 if first is not None else 0.0
    short = hist_days < c["baseline_days"]
    denom = min(float(c["baseline_days"]), max(hist_days, 1.0))
    base_r4: dict[str, int] = defaultdict(int)
    for e in base:
        assert e.lat is not None and e.lon is not None
        if str(e.attrs.get("confidence")) in confs and not is_static(e):
            base_r4[h3_cell(e.lat, e.lon, 4)] += 1

    r4_count: dict[str, int] = defaultdict(int)
    r6_groups: dict[str, list[GeoEvent]] = defaultdict(list)
    for e in win:
        assert e.lat is not None and e.lon is not None
        r4_count[h3_cell(e.lat, e.lon, 4)] += 1
        r6_groups[h3_cell(e.lat, e.lon, 6)].append(e)

    out: list[Evidence] = []
    for r6, grp in sorted(r6_groups.items()):
        lat0, lon0 = grp[0].lat, grp[0].lon
        assert lat0 is not None and lon0 is not None
        r4 = h3_cell(lat0, lon0, 4)
        n = len(grp)
        frp = sum(float(e.value or 0.0) for e in grp)
        base_mean = base_r4.get(r4, 0) / denom
        ratio = r4_count[r4] / (base_mean + 1.0)
        t1 = n >= c["min_detections"]
        t2 = frp >= c["frp_min_mw"]
        t3 = ratio >= c["ratio_min"]
        if not ((t1 or t2) and t3):
            continue
        passes = {_pass_key(e, c["pass_bin_min"]) for e in grp}
        cov_conf = _clamp(len(passes) / c["expected_passes"])
        score = _clamp(ratio / (2 * c["ratio_min"])) * cov_conf
        gaps: list[str] = []
        if short:
            gaps.append(f"short baseline: {hist_days:.0f} of {c['baseline_days']} days of history")
            score = min(score, c["short_baseline_cap"])
        if masked_r4.get(r4):
            gaps.append(f"static heat source masked: {masked_r4[r4]} detections")
        if len(passes) < c["expected_passes"]:
            gaps.append(f"{len(passes)} of {c['expected_passes']} expected satellite passes seen")
        lat = sum(float(e.lat or 0.0) for e in grp) / n
        lon = sum(float(e.lon or 0.0) for e in grp) / n
        top = sorted(grp, key=lambda e: (-(e.value or 0.0), e.id))[: c["max_records"]]
        if not t1:
            cf = (
                f"would not fire with < {c['min_detections']} detections and "
                f"< {c['frp_min_mw']:g} MW FRP, or ratio < {c['ratio_min']:g}"
            )
        else:
            cf = (
                f"would not fire with < {c['min_detections']} detections "
                f"or ratio < {c['ratio_min']:g}"
            )
        out.append(
            Evidence(
                id=evidence_id(AlertKind.THERMAL.value, r6, "", start, aoi.name),
                kind=AlertKind.THERMAL,
                detector=NAME,
                bands=[Band.NONE],
                detector_version=VERSION,
                aoi=aoi.name,
                window_start=start,
                window_end=end,
                h3_cell=r6,
                h3_res=6,
                lat=lat,
                lon=lon,
                score=score,
                rules=[
                    RuleResult(
                        rule="T1_min_detections",
                        description="Detections in the r6 cell within the window",
                        value=float(n),
                        threshold=float(c["min_detections"]),
                        op=">=",
                        passed=t1,
                    ),
                    RuleResult(
                        rule="T2_frp_sum",
                        description="Summed fire radiative power (MW) in the r6 cell",
                        value=frp,
                        threshold=float(c["frp_min_mw"]),
                        op=">=",
                        passed=t2,
                    ),
                    RuleResult(
                        rule="T3_baseline_ratio",
                        description=(
                            "r4 detections in window / (mean detections per day in baseline + 1)"
                        ),
                        value=ratio,
                        threshold=float(c["ratio_min"]),
                        op=">=",
                        passed=t3,
                        baseline=base_mean,
                    ),
                ],
                features={
                    "n_detections": float(n),
                    "sum_frp_mw": frp,
                    "max_frp_mw": max(float(e.value or 0.0) for e in grp),
                    "n_r4_detections": float(r4_count[r4]),
                    "baseline_mean_per_day": base_mean,
                    "ratio": ratio,
                    "n_passes": float(len(passes)),
                    "n_static_masked": float(masked_r4.get(r4, 0)),
                },
                records=[RecordRef(source=e.source, id=e.id, url=e.url) for e in top],
                counterfactual=cf,
                coverage=Coverage(
                    n_observed=len(passes),
                    n_quality=sum(1 for e in grp if e.attrs.get("confidence") == "high"),
                    providers=sorted({e.source for e in grp}),
                    confidence=cov_conf,
                ),
                gaps=gaps,
                created_at=now,
            )
        )
    return out
