"""Military orbit / loiter detector (racetracks, circles, figure-8s) over a 75-min window.

Assumptions
-----------
* Military = readsb `mil` flag OR ICAO type in `mil_types`. All source types are used: military
  aircraft are often MLAT-only, and orbit *shape* does not depend on GNSS integrity. MLAT-majority
  tracks get a coverage penalty (`mlat_factor`) because MLAT positions are noisier.
* Fixes are de-duplicated per timestamp and thinned to >= `resample_s` so that heading noise does
  not accumulate into a fake 360-degree turn. Heading changes smaller than `turn_deadband_deg` are
  ignored in the cumulative turn (a 1.5 deg/s standard-rate turn is ~45 deg per 30 s step).
* Heading = reported `track` when present, else the bearing from the previous fix. All heading
  math is circular (359 -> 1 is a 2-degree change).
* Novelty: the baseline for metric `orbit_active` holds one value per past tick in which an orbit
  was detected in the cell. prior_hours = n * step_min / 60, novelty = exp(-prior_hours / scale).
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from heimdall.detect.common import (
    Baseline,
    align_window,
    cfg_get,
    evidence_id,
    in_window,
    record_id,
)
from heimdall.geo import (
    angle_diff,
    circular_variance,
    displacement_ratio,
    h3_cell,
    haversine_km,
    initial_bearing,
)
from heimdall.schemas import AircraftObs, AlertKind, Band, Coverage, Evidence, RecordRef, RuleResult

NAME = "orbit"
VERSION = "orbit-1.0.1"
METRIC = "orbit_active"

DEFAULTS: dict[str, Any] = {
    "h3_res": 4,
    "window_min": 75,
    "step_min": 5,
    "min_points": 20,
    "resample_s": 30,
    "turn_deadband_deg": 3.0,
    "circ_var_min": 0.5,
    "cum_turn_min_deg": 360,
    "displacement_max": 0.25,
    "mil_types": [],
    "tanker_types": [],
    "jam_proximity_km": 100,
    "novelty_scale_h": 24.0,
    "mlat_factor": 0.8,
}


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def is_military(o: AircraftObs, mil_types: Sequence[str]) -> bool:
    return o.mil or (o.type_code is not None and o.type_code.upper() in mil_types)


def cumulative_turn(headings: Sequence[float], deadband: float) -> float:
    """Sum of |circular heading change| ignoring steps below `deadband` degrees."""
    total = 0.0
    for a, b in itertools.pairwise(headings):
        d = abs(angle_diff(a, b))
        if d >= deadband:
            total += d
    return total


def track_shape(fixes: Sequence[AircraftObs], c: dict[str, Any]) -> dict[str, float]:
    """Shape features of a (sorted, deduped) track: circ_var, cum_turn_deg, displacement ..."""
    thin: list[AircraftObs] = []
    for o in fixes:
        if not thin or (o.ts - thin[-1].ts).total_seconds() >= c["resample_s"]:
            thin.append(o)
    heads: list[float] = []
    for i, o in enumerate(thin):
        if o.track is not None:
            heads.append(o.track % 360.0)
        elif i > 0:
            p = thin[i - 1]
            heads.append(initial_bearing(p.lat, p.lon, o.lat, o.lon))
    pts = [(o.lat, o.lon) for o in thin]
    lat_c = sum(p[0] for p in pts) / len(pts)
    lon_c = sum(p[1] for p in pts) / len(pts)
    cum = cumulative_turn(heads, c["turn_deadband_deg"])
    return {
        "n_points": float(len(thin)),
        "circ_var": round(circular_variance(heads), 4),
        "cum_turn_deg": round(cum, 1),
        "laps": round(cum / 360.0, 2),
        "displacement": round(displacement_ratio(pts), 4),
        "centroid_lat": lat_c,
        "centroid_lon": lon_c,
        "radius_km": round(sum(haversine_km(p[0], p[1], lat_c, lon_c) for p in pts) / len(pts), 2),
        "duration_min": round((thin[-1].ts - thin[0].ts).total_seconds() / 60.0, 1),
        "mlat_frac": round(sum(1 for o in thin if not o.is_true_adsb) / len(thin), 3),
        "anomaly_first_ts": thin[0].ts.timestamp(),
        "anomaly_last_ts": thin[-1].ts.timestamp(),
    }


def detect(
    obs: Sequence[AircraftObs],
    cfg: dict[str, Any],
    now: datetime,
    baseline: Baseline | None = None,
    jam_evidence: Sequence[Evidence] = (),
    *,
    aoi: str | None = None,
) -> list[Evidence]:
    """One Evidence per military aircraft whose track in the window is a closed orbit:
    n_points >= min_points AND circ_var >= circ_var_min AND cum_turn >= cum_turn_min_deg AND
    displacement <= displacement_max."""
    c = _cfg(cfg)
    mil_types = [t.upper() for t in c["mil_types"]]
    tankers = {t.upper() for t in c["tanker_types"]}
    start, end = align_window(now, c["window_min"], c["step_min"])
    by_hex: dict[str, dict[datetime, AircraftObs]] = defaultdict(dict)
    raw_by_hex: dict[str, list[AircraftObs]] = defaultdict(list)
    for o in sorted(in_window(obs, start, end), key=lambda o: (o.ts, o.provider)):
        if o.on_ground or not is_military(o, mil_types):
            continue
        by_hex[o.hex].setdefault(o.ts, o)
        raw_by_hex[o.hex].append(o)
    jams = [j for j in jam_evidence if j.lat is not None and j.lon is not None]
    out: list[Evidence] = []
    for h, fixes_by_ts in sorted(by_hex.items()):
        fixes = [fixes_by_ts[t] for t in sorted(fixes_by_ts)]
        if len(fixes) < 2:
            continue
        f = track_shape(fixes, c)
        r_pts = RuleResult(
            rule="min_points",
            description=f"Resampled (>= {c['resample_s']} s) fixes in the window",
            value=f["n_points"],
            threshold=float(c["min_points"]),
            passed=f["n_points"] >= c["min_points"],
        )
        r_cv = RuleResult(
            rule="circ_var",
            description="Spread of headings (0 = one direction, 1 = all directions)",
            value=f["circ_var"],
            threshold=c["circ_var_min"],
            passed=f["circ_var"] >= c["circ_var_min"],
        )
        r_turn = RuleResult(
            rule="cum_turn",
            description="Total heading change in degrees (small wobbles ignored)",
            value=f["cum_turn_deg"],
            threshold=float(c["cum_turn_min_deg"]),
            passed=f["cum_turn_deg"] >= c["cum_turn_min_deg"],
        )
        r_d = RuleResult(
            rule="displacement",
            description="Start-to-end distance as a share of path flown (1 = straight, 0 = loop)",
            value=f["displacement"],
            threshold=c["displacement_max"],
            op="<=",
            passed=f["displacement"] <= c["displacement_max"],
        )
        rules = [r_pts, r_cv, r_turn, r_d]
        if not all(r.passed for r in rules):
            continue
        lat_c, lon_c = f["centroid_lat"], f["centroid_lon"]
        cell = h3_cell(lat_c, lon_c, c["h3_res"])
        type_code = next((o.type_code for o in fixes if o.type_code), None)
        is_tanker = type_code is not None and type_code.upper() in tankers
        gaps: list[str] = []
        # novelty vs baseline
        if baseline is None:
            novelty = 0.5
            gaps.append("no orbit baseline")
            prior_h = float("nan")
        else:
            prior_h = len(baseline(cell, METRIC)) * c["step_min"] / 60.0
            novelty = math.exp(-prior_h / c["novelty_scale_h"])
        # proximity to jamming
        jam_d = min(
            (haversine_km(lat_c, lon_c, j.lat, j.lon) for j in jams),  # type: ignore[arg-type]
            default=float("inf"),
        )
        near = jam_d <= c["jam_proximity_km"]
        rules.append(
            RuleResult(
                rule="near_jamming",
                description="Distance from orbit centroid to nearest jamming Evidence (km)",
                value=None if math.isinf(jam_d) else round(jam_d, 1),
                threshold=float(c["jam_proximity_km"]),
                op="<=",
                passed=near,
            )
        )
        conf = min(1.0, f["n_points"] / (2 * c["min_points"]))
        if f["mlat_frac"] > 0.5:
            conf *= c["mlat_factor"]
            gaps.append("MLAT-majority track (noisier positions)")
        providers = sorted({o.provider for o in raw_by_hex[h]})
        # providers whose own reports trace the orbit (>= min_points/2 fixes); else all
        by_prov: dict[str, list[AircraftObs]] = defaultdict(list)
        for o in sorted(raw_by_hex[h], key=lambda o: o.ts):
            by_prov[o.provider].append(o)
        # fallback: only providers that contributed fixes to the de-duplicated track itself
        showing = [p for p in providers if len(by_prov[p]) >= c["min_points"] / 2] or sorted(
            {o.provider for o in fixes}
        )
        feats = {k: v for k, v in f.items() if k not in ("centroid_lat", "centroid_lon")}
        feats.update(
            {
                "is_tanker": 1.0 if is_tanker else 0.0,
                "novelty": round(novelty, 4),
                "near_jamming": 1.0 if near else 0.0,
            }
        )
        if not math.isnan(prior_h):
            feats["prior_orbit_hours"] = round(prior_h, 2)
        if not math.isinf(jam_d):
            feats["jam_distance_km"] = round(jam_d, 1)
        shape = min(1.0, max(0.0, (f["laps"] - 1.0) / 2.0))
        cf = (
            "Would not count as an orbit if the aircraft held a steadier heading "
            f"(spread of headings now {f['circ_var']}, needs {c['circ_var_min']}), turned less "
            f"than {c['cum_turn_min_deg']} degrees in total (now {f['cum_turn_deg']}), or "
            f"ended up far from where it started (now {f['displacement']} of its path length, "
            f"limit {c['displacement_max']})."
        )
        step = max(1, len(fixes) // 60)
        geom = {
            "type": "LineString",
            "coordinates": [[round(o.lon, 5), round(o.lat, 5)] for o in fixes[::step]],
        }
        out.append(
            Evidence(
                id=evidence_id(AlertKind.MIL_ORBIT.value, cell, h, start, aoi),
                kind=AlertKind.MIL_ORBIT,
                detector=NAME,
                bands=[Band.NONE],
                detector_version=VERSION,
                aoi=aoi,
                window_start=start,
                window_end=end,
                h3_cell=cell,
                h3_res=c["h3_res"],
                lat=lat_c,
                lon=lon_c,
                geometry=geom,
                score=round(0.4 + 0.3 * novelty + 0.3 * shape, 4),
                rules=rules,
                features=feats,
                records=[
                    RecordRef(source=o.provider, id=record_id(o))
                    for p in showing
                    for o in (by_prov[p][0], by_prov[p][-1])
                ],
                entities=[h],
                counterfactual=cf,
                coverage=Coverage(
                    n_observed=1,
                    n_quality=1 if f["mlat_frac"] <= 0.5 else 0,
                    providers=providers,
                    confidence=round(conf, 4),
                ),
                gaps=gaps,
                created_at=now,
            )
        )
    return out
