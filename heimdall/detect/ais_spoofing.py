"""AIS spoofing detector over `VesselObs`: A1 jump, A2 identical positions, A3 ring, A4 circle.

Assumptions
-----------
* Invalid positions (out of range, exactly (0, 0)) are dropped first. Identical (mmsi, ts) fixes
  from several receivers are one fix.
* **A1 jump**: a pair is flagged when implied speed > `teleport_kn` *and* distance >
  `jump_min_km` (both: short hops at short dt give huge speeds from timestamp quantisation).
  A single out-and-back fix (both adjacent pairs flagged, the bridging pair clean) is a glitch and
  removed. A1 fires when flagged pairs (+ trailing clean pairs if only one jump) >= `persist_pairs`.
  **MMSI collision guard**: fixes that alternate between two self-consistent clusters more than
  `collision_km` (50) apart (>= 3 switches, no flagged pair inside a cluster) are two ships sharing
  an MMSI, not a spoof: no Evidence (see `mmsi_collisions`).
* **A2 identical**: >= `identical_min_vessels` distinct MMSIs at the same position rounded to
  `identical_decimals` (5 decimals ~ 1 m) within one `identical_bin_s` bin. Ships rafted 20 m
  apart differ at 5 decimals and never match.
* **A3 ring**: in one `ring_bin_s` bin (last fix per MMSI) >= `ring_min_vessels` MMSIs whose radial
  distances to their centroid have CV <= `ring_cv_max`, bearing circular variance >=
  `ring_bearing_circvar_min` and median radius in `ring_radius_km`. The test runs per
  *neighbourhood*, not over the whole AOI (background traffic would move the centroid): a ring of
  radius <= R_max fits in a square of side 4 R_max, so fixes are bucketed in 4 half-cell-shifted
  square grids (cell side 4 R_max, local km projection) and each bucket with enough vessels is
  tested; if the raw bucket fails, a RANSAC circle search (circumcircle of 3 vessels, radius in
  `ring_radius_km`, inliers within `ring_cv_max * R` of the circle; <= 600 seeded samples) picks
  the largest concentric set, which is then re-tested with the same CV/bearing/radius rules.
* **A4 circle**: one MMSI, >= `circle_min_fixes` fixes, Kasa least-squares circle fit with
  relative RMSE <= `circle_rel_rmse_max`, |net signed turn| >= `circle_min_turn_deg` (>= 2 laps:
  a purse-seine set is one lap) and radius <= `circle_max_radius_km`.
* Evidence carries `anomaly_first_ts`/`anomaly_last_ts` (epoch s) so the pipeline's persistence
  logic works; A1/A4 are per-vessel (`per_vessel` = 1, persistence subject = MMSI), A2/A3 per cell.
"""

from __future__ import annotations

import itertools
import math
import random
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
    local_xy_km,
    median,
    vessel_cell_of,
    vessel_in_window,
    vessel_record_id,
)
from heimdall.geo import (
    angle_diff,
    circular_variance,
    h3_cell,
    haversine_km,
    implied_speed_kt,
    initial_bearing,
)
from heimdall.schemas import (
    AlertKind,
    Coverage,
    Evidence,
    RecordRef,
    RuleResult,
    VesselObs,
)

NAME = "ais_spoofing"
VERSION = "ais-spoofing-1.1.0"

DEFAULTS: dict[str, Any] = {
    "h3_res": 4,
    "window_min": 60,
    "step_min": 5,
    "teleport_kn": 60,
    "jump_min_km": 5,
    "persist_pairs": 2,
    "span_cap_s": 300,
    "collision_km": 50,
    "collision_min_switches": 3,
    "identical_min_vessels": 3,
    "identical_decimals": 5,
    "identical_bin_s": 300,
    "ring_min_vessels": 5,
    "ring_bin_s": 300,
    "ring_cv_max": 0.1,
    "ring_bearing_circvar_min": 0.7,
    "ring_radius_km": [0.2, 30.0],
    "circle_min_fixes": 20,
    "circle_rel_rmse_max": 0.05,
    "circle_min_turn_deg": 720,
    "circle_max_radius_km": 5,
    "full_coverage_fixes": 20,
    "single_provider_factor": 0.8,
}


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def _flag(a: VesselObs, b: VesselObs, c: dict[str, Any]) -> bool:
    dt = (b.ts - a.ts).total_seconds()
    if dt <= 0:
        return False
    d = haversine_km(a.lat, a.lon, b.lat, b.lon)
    if d <= c["jump_min_km"]:
        return False
    return implied_speed_kt(a.lat, a.lon, 0.0, b.lat, b.lon, dt) > c["teleport_kn"]


def is_mmsi_collision(fixes: Sequence[VesselObs], c: dict[str, Any]) -> bool:
    """Two self-consistent clusters > collision_km apart, alternating (one MMSI, two ships)."""
    if len(fixes) < 4:
        return False
    a0 = fixes[0]
    far = [haversine_km(a0.lat, a0.lon, f.lat, f.lon) > c["collision_km"] for f in fixes]
    if not any(far):
        return False
    switches = sum(1 for x, y in itertools.pairwise(far) if x != y)
    if switches < c["collision_min_switches"]:
        return False
    for k, (a, b) in enumerate(itertools.pairwise(fixes)):
        if far[k] == far[k + 1] and _flag(a, b, c):
            return False  # a cluster is not self-consistent: a real jump, not two ships
    return True


def mmsi_collisions(obs: Sequence[VesselObs], cfg: dict[str, Any], now: datetime) -> set[int]:
    """MMSIs whose window fixes look like two ships sharing one MMSI (diagnostic, no Evidence)."""
    c = _cfg(cfg)
    start, end = align_window(now, c["window_min"], c["step_min"])
    by: dict[int, list[VesselObs]] = defaultdict(list)
    for o in dedupe_vessel_fixes(vessel_in_window(obs, start, end)):
        by[o.mmsi].append(o)
    return {m for m, fx in by.items() if is_mmsi_collision(fx, c)}


def _jump(fixes: list[VesselObs], c: dict[str, Any]) -> dict[str, Any] | None:
    if len(fixes) < 2:
        return None
    flags = [_flag(a, b, c) for a, b in itertools.pairwise(fixes)]
    glitch = {
        i
        for i in range(1, len(fixes) - 1)
        if flags[i - 1] and flags[i] and not _flag(fixes[i - 1], fixes[i + 1], c)
    }
    clean = [o for i, o in enumerate(fixes) if i not in glitch]
    flagged = [k for k, (a, b) in enumerate(itertools.pairwise(clean)) if _flag(a, b, c)]
    if not flagged:
        return None
    trailing = len(clean) - 2 - flagged[-1]
    persisted = len(flagged) + (trailing if len(flagged) == 1 else 0)
    if persisted < c["persist_pairs"]:
        return None
    cap = timedelta(seconds=c["span_cap_s"])
    anomalous: list[VesselObs] = []
    for k in flagged:
        t0 = clean[k + 1].ts
        anomalous += [o for o in clean[k + 1 :] if o.ts - t0 < cap]
    k0 = flagged[0]
    a, b = clean[k0], clean[k0 + 1]
    vmax = max(
        implied_speed_kt(
            clean[k].lat,
            clean[k].lon,
            0.0,
            clean[k + 1].lat,
            clean[k + 1].lon,
            (clean[k + 1].ts - clean[k].ts).total_seconds(),
        )
        for k in flagged
    )
    return {
        "anchor": a,
        "displaced": b,
        "anomalous": list({o.ts: o for o in [a, *anomalous]}.values()),
        "persisted": float(persisted),
        "jump_km": haversine_km(a.lat, a.lon, b.lat, b.lon),
        "vmax": vmax,
        "n_glitches": float(len(glitch)),
    }


def _solve3(m: list[list[float]], y: list[float]) -> list[float] | None:
    a = [[*row, v] for row, v in zip(m, y, strict=True)]
    for i in range(3):
        p = max(range(i, 3), key=lambda r: abs(a[r][i]))
        if abs(a[p][i]) < 1e-12:
            return None
        a[i], a[p] = a[p], a[i]
        for r in range(3):
            if r != i:
                f = a[r][i] / a[i][i]
                a[r] = [x - f * z for x, z in zip(a[r], a[i], strict=True)]
    return [a[i][3] / a[i][i] for i in range(3)]


def fit_circle(xy: Sequence[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    """Kasa fit -> (cx, cy, radius, relative RMSE); None if degenerate (collinear/identical)."""
    n = len(xy)
    mx = sum(x for x, _ in xy) / n
    my = sum(y for _, y in xy) / n
    pts = [(x - mx, y - my) for x, y in xy]
    sxx = sum(x * x for x, _ in pts)
    syy = sum(y * y for _, y in pts)
    sxy = sum(x * y for x, y in pts)
    sx = sum(x for x, _ in pts)
    sy = sum(y for _, y in pts)
    r2 = [x * x + y * y for x, y in pts]
    bx = sum(x * r for (x, _), r in zip(pts, r2, strict=True))
    by = sum(y * r for (_, y), r in zip(pts, r2, strict=True))
    rhs = [bx, by, sum(r2)]
    sol = _solve3([[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, float(n)]], rhs)
    if sol is None:
        return None
    a, b, cc = sol[0] / 2, sol[1] / 2, sol[2]  # x^2 + y^2 = 2ax + 2by + cc (linear in D, E, cc)
    rad2 = cc + a * a + b * b
    if rad2 <= 1e-12:
        return None
    rad = math.sqrt(rad2)
    resid = [math.hypot(x - a, y - b) - rad for x, y in pts]
    rmse = math.sqrt(sum(e * e for e in resid) / n)
    return a + mx, b + my, rad, rmse / rad


def _circle(fixes: list[VesselObs], c: dict[str, Any]) -> dict[str, Any] | None:
    if len(fixes) < c["circle_min_fixes"]:
        return None
    lat0, lon0 = fixes[0].lat, fixes[0].lon
    fit = fit_circle([local_xy_km(o.lat, o.lon, lat0, lon0) for o in fixes])
    if fit is None:
        return None
    _cx, _cy, rad, rel = fit
    if rel > c["circle_rel_rmse_max"] or rad > c["circle_max_radius_km"]:
        return None
    # net signed turn of the segment bearings (one direction for a circle; jitter cancels)
    brg = [
        initial_bearing(a.lat, a.lon, b.lat, b.lon)
        for a, b in itertools.pairwise(fixes)
        if (a.lat, a.lon) != (b.lat, b.lon)
    ]
    turn = sum(angle_diff(a, b) for a, b in itertools.pairwise(brg))
    return {"radius_km": rad, "rel_rmse": rel, "turn_deg": abs(turn), "anomalous": list(fixes)}


def _ring(bin_fixes: dict[int, VesselObs], c: dict[str, Any]) -> dict[str, Any] | None:
    if len(bin_fixes) < c["ring_min_vessels"]:
        return None
    pts = list(bin_fixes.values())
    lat_c = sum(p.lat for p in pts) / len(pts)
    lon_c = sum(p.lon for p in pts) / len(pts)
    rad = [haversine_km(lat_c, lon_c, p.lat, p.lon) for p in pts]
    mean_r = sum(rad) / len(rad)
    if mean_r <= 0:
        return None
    cv = math.sqrt(sum((r - mean_r) ** 2 for r in rad) / len(rad)) / mean_r
    circvar = circular_variance([initial_bearing(lat_c, lon_c, p.lat, p.lon) for p in pts])
    med_r = median(rad)
    lo, hi = c["ring_radius_km"]
    if cv <= c["ring_cv_max"] and circvar >= c["ring_bearing_circvar_min"] and lo <= med_r <= hi:
        return {
            "lat": lat_c,
            "lon": lon_c,
            "cv": cv,
            "circvar": circvar,
            "radius_km": med_r,
            "fixes": pts,
        }
    return None


_RING_SAMPLES = 600  # max circumcircle candidates per neighbourhood (exhaustive when fewer)


def _circle_inliers(xy: dict[int, tuple[float, float]], c: dict[str, Any]) -> list[int]:
    """RANSAC circle: the largest set of points within `ring_cv_max * R` of a circle through three
    of them, with R in `ring_radius_km`. Deterministic (fixed seed; exhaustive if few)."""
    ids = sorted(xy)
    lo, hi = c["ring_radius_km"]
    triples: Sequence[tuple[int, int, int]]
    if math.comb(len(ids), 3) <= _RING_SAMPLES:
        triples = list(itertools.combinations(ids, 3))
    else:
        rng = random.Random(len(ids))
        triples = [tuple(sorted(rng.sample(ids, 3))) for _ in range(_RING_SAMPLES)]  # type: ignore[misc]
    best: list[int] = []
    for a, b, d in triples:
        (x1, y1), (x2, y2), (x3, y3) = xy[a], xy[b], xy[d]
        det = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
        if abs(det) < 1e-9:
            continue
        s1, s2, s3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
        cx = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / det
        cy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / det
        rad = math.hypot(x1 - cx, y1 - cy)
        if not lo <= rad <= hi:
            continue
        tol = c["ring_cv_max"] * rad
        inl = [m for m in ids if abs(math.hypot(xy[m][0] - cx, xy[m][1] - cy) - rad) <= tol]
        if len(inl) > len(best):
            best = inl
    return best


def _rings_in_bin(bin_fixes: dict[int, VesselObs], c: dict[str, Any]) -> list[dict[str, Any]]:
    """Rings among one bin's last fixes, found per neighbourhood (see module docstring, A3)."""
    if len(bin_fixes) < c["ring_min_vessels"]:
        return []
    side = 4.0 * float(c["ring_radius_km"][1])
    proj = {
        m: (o.lon * 111.32 * math.cos(math.radians(o.lat)), o.lat * 110.57)
        for m, o in bin_fixes.items()
    }
    buckets: dict[tuple[int, int, int], dict[int, VesselObs]] = defaultdict(dict)
    for m, o in bin_fixes.items():
        x, y = proj[m]
        for sx in (0, 1):
            for sy in (0, 1):
                key = (
                    sx * 2 + sy,
                    math.floor(x / side + 0.5 * sx),
                    math.floor(y / side + 0.5 * sy),
                )
                buckets[key][m] = o
    found: list[dict[str, Any]] = []
    used: set[frozenset[int]] = set()
    for _, members in sorted(buckets.items()):
        if len(members) < c["ring_min_vessels"]:
            continue
        r = _ring(members, c)
        if r is None:  # background traffic in the neighbourhood: look for a circle inside it
            inl = _circle_inliers({m: proj[m] for m in members}, c)
            if len(inl) >= c["ring_min_vessels"]:
                r = _ring({m: members[m] for m in inl}, c)
        if r is not None:
            ids = frozenset(o.mmsi for o in r["fixes"])
            if ids not in used:
                used.add(ids)
                found.append(r)
    return found


def _cov(
    cell_mmsis: dict[str, set[int]], cell: str, fixes: Sequence[VesselObs], c: dict[str, Any]
) -> tuple[Coverage, list[str]]:
    prov = sorted({o.provider for o in fixes})
    conf = min(1.0, len(fixes) / c["full_coverage_fixes"])
    gaps: list[str] = []
    if len(prov) <= 1:
        conf *= c["single_provider_factor"]
        gaps.append("single provider")
    n = len(cell_mmsis.get(cell, ())) or 1
    return Coverage(n_observed=n, n_quality=n, providers=prov, confidence=round(conf, 4)), gaps


def detect(
    obs: Sequence[VesselObs],
    cfg: dict[str, Any],
    now: datetime,
    baseline: Baseline | None = None,
    *,
    aoi: str | None = None,
) -> list[Evidence]:
    """Return AIS spoofing Evidence for the window ending at the aligned `now`. Pure."""
    c = _cfg(cfg)
    start, end = align_window(now, c["window_min"], c["step_min"])
    fixes_all = dedupe_vessel_fixes(vessel_in_window(obs, start, end))
    res = c["h3_res"]
    by_mmsi: dict[int, list[VesselObs]] = defaultdict(list)
    cell_mmsis: dict[str, set[int]] = defaultdict(set)
    for o in fixes_all:
        by_mmsi[o.mmsi].append(o)
        cell_mmsis[vessel_cell_of(o, res)].add(o.mmsi)

    evs: list[Evidence] = []

    def emit(
        signal: str,
        cell: str,
        entity: str,
        lat: float,
        lon: float,
        score: float,
        rules: list[RuleResult],
        feats: dict[str, float],
        anomalous: Sequence[VesselObs],
        cf: str,
        mmsis: Sequence[int],
        geom: dict[str, Any] | None = None,
        per_vessel: bool = False,
    ) -> None:
        cov, gaps = _cov(cell_mmsis, cell, anomalous, c)
        feats = dict(feats)
        feats[signal] = 1.0
        feats["per_vessel"] = 1.0 if per_vessel else 0.0
        feats["anomaly_first_ts"] = min(o.ts for o in anomalous).timestamp()
        feats["anomaly_last_ts"] = max(o.ts for o in anomalous).timestamp()
        recs = list({vessel_record_id(o) + o.provider: o for o in anomalous}.values())[:40]
        evs.append(
            Evidence(
                id=evidence_id(
                    AlertKind.AIS_SPOOFING.value, cell, f"{signal}:{entity}", start, aoi
                ),
                kind=AlertKind.AIS_SPOOFING,
                detector=NAME,
                detector_version=VERSION,
                aoi=aoi,
                window_start=start,
                window_end=end,
                h3_cell=cell,
                h3_res=res,
                lat=lat,
                lon=lon,
                geometry=geom,
                score=round(max(0.0, min(1.0, score)), 4),
                rules=rules,
                features=feats,
                records=[RecordRef(source=o.provider, id=vessel_record_id(o)) for o in recs],
                entities=[str(m) for m in mmsis],
                counterfactual=cf,
                coverage=cov,
                gaps=gaps,
                created_at=now,
            )
        )

    # ---- A1 jump (per vessel, collision-guarded) and A4 circle
    for mmsi, fx in sorted(by_mmsi.items()):
        if is_mmsi_collision(fx, c):
            continue
        j = _jump(fx, c)
        if j is not None:
            a, b = j["anchor"], j["displaced"]
            rules = [
                RuleResult(
                    rule="A1_jump",
                    description=(
                        f"Position jumps faster than {c['teleport_kn']} kn and farther than "
                        f"{c['jump_min_km']} km, for at least {c['persist_pairs']} consecutive "
                        "reports (one-off glitches removed)"
                    ),
                    value=j["persisted"],
                    threshold=float(c["persist_pairs"]),
                    passed=True,
                ),
                RuleResult(
                    rule="A1_speed",
                    description="Fastest speed implied by the jump (kn)",
                    value=round(j["vmax"], 1),
                    threshold=float(c["teleport_kn"]),
                    op=">",
                    passed=True,
                ),
            ]
            feats = {
                "jump_km": round(j["jump_km"], 2),
                "max_implied_kn": round(j["vmax"], 1),
                "s1_persisted_pairs": j["persisted"],
                "n_glitches": j["n_glitches"],
            }
            emit(
                "A1",
                vessel_cell_of(a, res),
                str(mmsi),
                a.lat,
                a.lon,
                0.5 + 0.5 * min(1.0, j["vmax"] / (10 * c["teleport_kn"])),
                rules,
                feats,
                j["anomalous"],
                f"Would not fire if the {j['jump_km']:.0f} km jump reverted within one fix "
                "(glitch) or two ships share this MMSI.",
                [mmsi],
                {"type": "LineString", "coordinates": [[a.lon, a.lat], [b.lon, b.lat]]},
                True,
            )
        cr = _circle(fx, c)
        if cr is not None and cr["turn_deg"] >= c["circle_min_turn_deg"]:
            mid = fx[len(fx) // 2]
            rules = [
                RuleResult(
                    rule="A4_circle_fit",
                    description="How far the track strays from a perfect circle (share of radius)",
                    value=round(cr["rel_rmse"], 4),
                    threshold=float(c["circle_rel_rmse_max"]),
                    op="<=",
                    passed=True,
                ),
                RuleResult(
                    rule="A4_turn",
                    description="Total turn in one direction (degrees)",
                    value=round(cr["turn_deg"], 1),
                    threshold=float(c["circle_min_turn_deg"]),
                    passed=True,
                ),
                RuleResult(
                    rule="A4_radius",
                    description="Fitted radius (km)",
                    value=round(cr["radius_km"], 3),
                    threshold=float(c["circle_max_radius_km"]),
                    op="<=",
                    passed=True,
                ),
            ]
            emit(
                "A4",
                vessel_cell_of(mid, res),
                str(mmsi),
                mid.lat,
                mid.lon,
                min(1.0, 0.6 + 0.4 * cr["turn_deg"] / (2 * c["circle_min_turn_deg"])),
                rules,
                {
                    "radius_km": round(cr["radius_km"], 3),
                    "rel_rmse": round(cr["rel_rmse"], 4),
                    "turn_deg": round(cr["turn_deg"], 1),
                    "laps": round(cr["turn_deg"] / 360, 2),
                },
                cr["anomalous"],
                f"Would not fire on a single lap (under {c['circle_min_turn_deg']} deg), e.g. a "
                "fishing net set, or a track that does not fit a circle.",
                [mmsi],
                None,
                True,
            )

    # ---- A2 identical positions per time bin
    ndec = int(c["identical_decimals"])
    bin_s = int(c["identical_bin_s"])
    groups: dict[tuple[int, float, float], dict[int, VesselObs]] = defaultdict(dict)
    for o in fixes_all:
        key = (int(o.ts.timestamp() // bin_s), round(o.lat, ndec), round(o.lon, ndec))
        groups[key].setdefault(o.mmsi, o)
    by_pos: dict[tuple[float, float], list[VesselObs]] = defaultdict(list)
    for (_, la, lo), members in groups.items():
        if len(members) >= c["identical_min_vessels"]:
            by_pos[(la, lo)] += members.values()
    for (la, lo), fx in sorted(by_pos.items()):
        ms = sorted({o.mmsi for o in fx})
        n = len(ms)
        emit(
            "A2",
            h3_cell(la, lo, res),
            f"{la},{lo}",
            la,
            lo,
            0.6 + 0.4 * min(1.0, (n - c["identical_min_vessels"] + 1) / 5),
            [
                RuleResult(
                    rule="A2_identical",
                    description=(
                        f"Different vessel IDs reporting the exact same position within {bin_s} s"
                    ),
                    value=float(n),
                    threshold=float(c["identical_min_vessels"]),
                    passed=True,
                )
            ],
            {"n_vessels": float(n)},
            fx,
            f"Would not fire with fewer than {c['identical_min_vessels']} vessel IDs, or if the "
            "positions differed by a few metres (ships moored side by side).",
            ms,
        )

    # ---- A3 ring per time bin
    rb = int(c["ring_bin_s"])
    ring_bins: dict[int, dict[int, VesselObs]] = defaultdict(dict)
    for o in fixes_all:  # last fix per MMSI per bin (fixes_all is time-ordered per MMSI)
        ring_bins[int(o.ts.timestamp() // rb)][o.mmsi] = o
    ring_hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for _, members in sorted(ring_bins.items()):
        for r in _rings_in_bin(members, c):
            ring_hits[h3_cell(r["lat"], r["lon"], res)].append(r)
    for cell, hits in sorted(ring_hits.items()):
        best = max(hits, key=lambda h: len(h["fixes"]))
        fx = [o for h in hits for o in h["fixes"]]
        ms = sorted({o.mmsi for o in fx})
        emit(
            "A3",
            cell,
            "ring",
            best["lat"],
            best["lon"],
            0.7 + 0.3 * min(1.0, len(ms) / (2 * c["ring_min_vessels"])),
            [
                RuleResult(
                    rule="A3_ring_cv",
                    description="Ships sit at almost the same distance from a common centre",
                    value=round(best["cv"], 4),
                    threshold=float(c["ring_cv_max"]),
                    op="<=",
                    passed=True,
                ),
                RuleResult(
                    rule="A3_ring_bearing",
                    description="Ships are spread evenly all around that centre",
                    value=round(best["circvar"], 3),
                    threshold=float(c["ring_bearing_circvar_min"]),
                    passed=True,
                ),
                RuleResult(
                    rule="A3_ring_vessels",
                    description="Vessel IDs on the ring at the same moment",
                    value=float(len(best["fixes"])),
                    threshold=float(c["ring_min_vessels"]),
                    passed=True,
                ),
            ],
            {
                "ring_radius_km": round(best["radius_km"], 3),
                "ring_cv": round(best["cv"], 4),
                "ring_bearing_circvar": round(best["circvar"], 3),
                "n_vessels": float(len(ms)),
                "ring_bins": float(len(hits)),
            },
            fx,
            "Would not fire for a tight cluster, ships at uneven distances from a centre, "
            f"or fewer than {c['ring_min_vessels']} vessel IDs.",
            ms,
        )
    return evs
