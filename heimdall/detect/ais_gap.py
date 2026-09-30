"""AIS dark-gap detector: a vessel stops transmitting where reception is demonstrably good.

Assumptions
-----------
* Lookback is `window_h` (24 h) ending at the step-aligned `now`. A gap is the interval between
  two consecutive fixes of one MMSI with dt >= `gap_min_h`, or *open*: no fix from the last fix
  until `now` (>= `gap_min_h`). A closed gap is reported only if it ended within
  `closed_report_h` (a gap 20 h old is history, not a live anomaly). An MMSI never seen in the
  window is invisible by construction.
* A vessel moored/at anchor (nav_status 1 or 5) and slow (sog < 0.5 kn; unknown sog counts as
  slow) at its last fix switches its transponder off legitimately: skipped.
* **Coverage test** (a gap in a hole in the receiver network is not a gap): the gap is cut into
  hourly bins from the last fix; a bin is *covered* if >= `gap_min_neighbors` other MMSIs were
  heard inside it within `gap_neighbor_km` of the last fix. Evidence only when the covered
  fraction >= `gap_covered_frac`; otherwise the gap is reported by `find_gaps` as
  "no reception evidence" and produces nothing ("absence of evidence" is never an alert).
* score = min(1, gap_h / 12) * covered_frac; coverage confidence = min(1, median neighbours per
  covered bin / (2 * gap_min_neighbors)).
* Per-vessel Evidence (`per_vessel` = 1); `anomaly_first_ts` = last fix before the gap and
  `anomaly_last_ts` = resume fix (closed) or window end (open), so an open gap accumulates
  persistence tick by tick.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from heimdall.detect.common import (
    Baseline,
    align_window,
    cfg_get,
    dedupe_vessel_fixes,
    evidence_id,
    median,
    vessel_cell_of,
    vessel_in_window,
    vessel_record_id,
)
from heimdall.geo import KT_PER_KMH, haversine_km
from heimdall.schemas import AlertKind, Coverage, Evidence, RecordRef, RuleResult, VesselObs

NAME = "ais_gap"
VERSION = "ais-gap-1.0.0"

DEFAULTS: dict[str, Any] = {
    "h3_res": 4,
    "window_h": 24,
    "step_min": 5,
    "gap_min_h": 2,
    "gap_min_neighbors": 3,
    "gap_neighbor_km": 20,
    "gap_covered_frac": 0.5,
    "closed_report_h": 6,
    "anchored_nav_status": [1, 5],
    "anchored_sog_kn": 0.5,
}

_GRID_DEG = 0.5  # >= 2 * neighbour radius at any latitude below ~70 deg with a 3x3 search


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


class _Index:
    """Coarse lat/lon grid over all window fixes for neighbour queries."""

    def __init__(self, fixes: Sequence[VesselObs]) -> None:
        self.grid: dict[tuple[int, int], list[VesselObs]] = defaultdict(list)
        for o in fixes:
            self.grid[self._key(o.lat, o.lon)].append(o)

    @staticmethod
    def _key(lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / _GRID_DEG), math.floor(lon / _GRID_DEG)

    def near(self, lat: float, lon: float, km: float) -> list[VesselObs]:
        ki, kj = self._key(lat, lon)
        out: list[VesselObs] = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out += [
                    o
                    for o in self.grid.get((ki + di, kj + dj), ())
                    if haversine_km(lat, lon, o.lat, o.lon) <= km
                ]
        return out


def _anchored(o: VesselObs, c: dict[str, Any]) -> bool:
    return o.nav_status in c["anchored_nav_status"] and (
        o.sog is None or o.sog < c["anchored_sog_kn"]
    )


def find_gaps(obs: Sequence[VesselObs], cfg: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """All candidate gaps with a decision: `fire` True/False and `reason` (diagnostic, pure).

    Reasons: "ok", "anchored", "no reception evidence".
    """
    c = _cfg(cfg)
    _, end = align_window(now, c["window_h"] * 60, c["step_min"])
    start = end - timedelta(hours=c["window_h"])
    fixes = dedupe_vessel_fixes(vessel_in_window(obs, start, end))
    by: dict[int, list[VesselObs]] = defaultdict(list)
    for o in fixes:
        by[o.mmsi].append(o)
    index = _Index(fixes)
    min_gap = timedelta(hours=c["gap_min_h"])
    recent = end - timedelta(hours=c["closed_report_h"])
    out: list[dict[str, Any]] = []
    for mmsi, fx in sorted(by.items()):
        segs: list[tuple[VesselObs, VesselObs | None, datetime]] = [
            (a, b, b.ts) for a, b in itertools.pairwise(fx) if b.ts - a.ts >= min_gap
        ]
        segs = [s for s in segs if s[2] >= recent]
        if end - fx[-1].ts >= min_gap:
            segs.append((fx[-1], None, end))
        for a, b, t_end in segs:
            gap_h = (t_end - a.ts).total_seconds() / 3600.0
            rec: dict[str, Any] = {
                "mmsi": mmsi,
                "last": a,
                "next": b,
                "gap_h": gap_h,
                "open": b is None,
                "end": t_end,
                "dist_km": haversine_km(a.lat, a.lon, b.lat, b.lon) if b else 0.0,
                "covered_frac": 0.0,
                "neighbors_median": 0.0,
                "n_nearby": 0,
            }
            rec["implied_speed_kn"] = rec["dist_km"] / gap_h * KT_PER_KMH
            if _anchored(a, c):
                rec.update(fire=False, reason="anchored")
                out.append(rec)
                continue
            near = [
                o
                for o in index.near(a.lat, a.lon, c["gap_neighbor_km"])
                if o.mmsi != mmsi and a.ts < o.ts < t_end
            ]
            n_bins = max(1, int(gap_h))
            per_bin: list[set[int]] = [set() for _ in range(n_bins)]
            for o in near:
                k = int((o.ts - a.ts).total_seconds() // 3600)
                if k < n_bins:
                    per_bin[k].add(o.mmsi)
            covered = [len(s) for s in per_bin if len(s) >= c["gap_min_neighbors"]]
            rec["covered_frac"] = len(covered) / n_bins
            rec["neighbors_median"] = median([float(x) for x in covered])
            rec["n_nearby"] = len({o.mmsi for o in near})
            ok = rec["covered_frac"] >= c["gap_covered_frac"]
            rec.update(fire=ok, reason="ok" if ok else "no reception evidence")
            out.append(rec)
    return out


def detect(
    obs: Sequence[VesselObs],
    cfg: dict[str, Any],
    now: datetime,
    baseline: Baseline | None = None,
    *,
    aoi: str | None = None,
) -> list[Evidence]:
    """Return AIS-gap Evidence for gaps that pass the anchored and coverage tests. Pure."""
    c = _cfg(cfg)
    _, end = align_window(now, c["window_h"] * 60, c["step_min"])
    start = end - timedelta(hours=c["window_h"])
    res = c["h3_res"]
    evs: list[Evidence] = []
    for g in find_gaps(obs, cfg, now):
        if not g["fire"]:
            continue
        a: VesselObs = g["last"]
        b: VesselObs | None = g["next"]
        cell = vessel_cell_of(a, res)
        score = min(1.0, g["gap_h"] / 12.0) * g["covered_frac"]
        feats = {
            "gap_h": round(g["gap_h"], 2),
            "open": 1.0 if g["open"] else 0.0,
            "dist_km": round(g["dist_km"], 2),
            "implied_speed_kn": round(g["implied_speed_kn"], 1),
            "covered_frac": round(g["covered_frac"], 3),
            "neighbors_median": g["neighbors_median"],
            "per_vessel": 1.0,
            "anomaly_first_ts": a.ts.timestamp(),
            "anomaly_last_ts": g["end"].timestamp(),
        }
        recs = [a] + ([b] if b is not None else [])
        evs.append(
            Evidence(
                id=evidence_id(
                    AlertKind.AIS_GAP.value, cell, f"{a.mmsi}:{int(a.ts.timestamp())}", start, aoi
                ),
                kind=AlertKind.AIS_GAP,
                detector=NAME,
                detector_version=VERSION,
                aoi=aoi,
                window_start=start,
                window_end=end,
                h3_cell=cell,
                h3_res=res,
                lat=a.lat,
                lon=a.lon,
                score=round(score, 4),
                rules=[
                    RuleResult(
                        rule="gap_h",
                        description="Hours without an AIS fix (open or between two fixes)",
                        value=round(g["gap_h"], 2),
                        threshold=float(c["gap_min_h"]),
                        passed=True,
                    ),
                    RuleResult(
                        rule="reception_coverage",
                        description=(
                            f"Fraction of hourly bins with >= {c['gap_min_neighbors']} other "
                            f"MMSIs heard within {c['gap_neighbor_km']} km of the last fix"
                        ),
                        value=round(g["covered_frac"], 3),
                        threshold=float(c["gap_covered_frac"]),
                        passed=True,
                    ),
                    RuleResult(
                        rule="not_anchored",
                        description="Last fix was not moored/anchored and slow",
                        value=float(a.nav_status) if a.nav_status is not None else None,
                        threshold=None,
                        op="!=",
                        passed=True,
                    ),
                ],
                features=feats,
                records=[RecordRef(source=o.provider, id=vessel_record_id(o)) for o in recs],
                entities=[str(a.mmsi)],
                counterfactual=(
                    f"Would not fire if the gap were < {c['gap_min_h']} h, the vessel were "
                    "moored/at anchor at its last fix, or fewer than "
                    f"{c['gap_min_neighbors']} neighbours were heard nearby (coverage hole)."
                ),
                coverage=Coverage(
                    n_observed=g["n_nearby"],
                    n_quality=g["n_nearby"],
                    providers=sorted({o.provider for o in recs}),
                    confidence=round(
                        min(1.0, g["neighbors_median"] / (2 * c["gap_min_neighbors"])), 4
                    ),
                ),
                gaps=["open gap: vessel may resume"] if g["open"] else [],
                created_at=now,
            )
        )
    return evs
