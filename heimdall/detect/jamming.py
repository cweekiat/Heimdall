"""GNSS jamming detector (GPSJam-style degraded-aircraft ratio per H3 cell).

Assumptions
-----------
* Only true ADS-B reports (`adsb_icao`, `adsb_icao_nt`) carry NIC/NACp that reflect the
  aircraft's own GNSS solution. MLAT/TIS-B/ADS-R positions are computed on the ground and are
  excluded from the ratio (they still count toward `coverage.n_observed`). A cell with only
  MLAT traffic therefore has `total == 0` and can never fire.
* Reports where both NIC and NACp are missing are *unknown*, not good: they do not enter the
  aircraft's dwell fraction.
* The same airframe seen by several aggregators is one aircraft (keyed by ICAO hex).
* Emitter localisation (1.2.0) assumes a fixed ground emitter with line-of-sight (radio-horizon)
  coverage d = 4.12 (sqrt(h) + sqrt(h_j)) km. LOS is an *upper bound* on real (power-limited)
  jamming range, so the fit is a coarse bearing/region estimate, never a survey-grade fix.
* The detector is a pure function of the window. Hysteresis state (which cells were active in
  the previous step) is passed in by the caller via `active`.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from heimdall.detect.common import (
    Baseline,
    align_window,
    cell_of,
    cfg_get,
    ellipse_2sigma,
    evidence_id,
    in_window,
    record_id,
    robust_z,
)
from heimdall.geo import EARTH_RADIUS_KM, h3_cell, h3_center, h3_disk
from heimdall.schemas import AircraftObs, AlertKind, Band, Coverage, Evidence, RecordRef, RuleResult

NAME = "jamming"
VERSION = "jamming-1.2.0"
METRIC = "jam_ratio"  # baseline metric key recorded per cell by the pipeline

DEFAULTS: dict[str, Any] = {
    "h3_res": 4,
    "window_min": 30,
    "step_min": 5,
    "nic_bad_below": 7,
    "nacp_bad_below": 8,
    "bad_dwell_frac": 0.5,
    "min_aircraft": 5,
    "ratio_alert": 0.10,
    "ratio_clear": 0.06,
    "robust_z": 3.0,
    "mad_floor": 0.02,
    "baseline_min_samples": 288,
    "full_coverage_aircraft": 15,
    "single_provider_factor": 0.8,
    "confidence_cap": 1.0,  # < 1 for sparse-traffic profiles (lower min_aircraft)
    # --- emitter localisation (radio-horizon fit, 1.2.0); priors, not backtested
    "loc_enabled": True,
    "loc_min_degraded": 3,  # bad aircraft (with altitude) needed, else centroid fallback
    "loc_min_healthy": 2,  # healthy aircraft (with altitude) needed for the outside test
    "loc_degraded_radius_km": 200.0,  # degraded fixes used: those within this of the centroid
    "loc_radius_km": 400.0,  # candidate r6 cells within this of the degraded-fix centroid
    "loc_res": 6,
    "loc_max_candidates": 16000,  # cap; radius shrinks to fit
    "loc_jammer_height_m": 10.0,
    "loc_horizon_km": 4.12,  # km per sqrt(m)
    "loc_top_k": 25,  # minimum cells forming the uncertainty region
    "loc_agreement_tol": 0.03,  # cells within this of the best agreement form the region
    "loc_min_agreement": 0.6,  # below this the fit is rejected -> centroid
    "loc_alt_band_m": 3000.0,  # healthy fixes must be within this of a degraded fix altitude
    "loc_max_fixes_per_ac": 6,
    "loc_max_healthy_ac": 100,
    "loc_healthy_radius_km": 800.0,
    "loc_soft_weight": 0.05,  # margin tie-break weight (resolves score plateaus)
}


@dataclass
class AircraftQuality:
    hex: str
    n_known: int = 0
    n_bad: int = 0
    first_bad: AircraftObs | None = None
    last_bad: AircraftObs | None = None
    by_provider: dict[str, list[int]] = field(default_factory=dict)  # provider -> [known, bad]
    first_bad_by_provider: dict[str, AircraftObs] = field(default_factory=dict)

    def showing_providers(self, frac: float) -> list[str]:
        """Providers whose *own* reports make this aircraft bad (used for Admiralty)."""
        return sorted(p for p, (k, b) in self.by_provider.items() if k and b / k >= frac)

    def is_bad(self, frac: float) -> bool:
        return self.n_known > 0 and self.n_bad / self.n_known >= frac


@dataclass
class CellStats:
    cell: str
    n_observed: int  # distinct airborne hexes, any source type
    total: int  # true-ADS-B hexes with at least one NIC/NACp value
    bad: int
    ratio: float  # de-noised R' = max(0, bad - 1) / total
    providers: list[str]
    agreement: float  # provider agreement on the bad aircraft (1.0 = all agree)
    bad_aircraft: list[AircraftQuality]


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def _degraded(o: AircraftObs, nic_below: int, nacp_below: int) -> bool | None:
    if o.nic is None and o.nac_p is None:
        return None
    return (o.nic is not None and o.nic < nic_below) or (
        o.nac_p is not None and o.nac_p < nacp_below
    )


def cell_stats(
    obs: Sequence[AircraftObs], cfg: dict[str, Any], now: datetime
) -> dict[str, CellStats]:
    """Per-cell degraded ratio for the aligned window ending at floor(now). Pure."""
    c = _cfg(cfg)
    start, end = align_window(now, c["window_min"], c["step_min"])
    seen: dict[str, set[str]] = defaultdict(set)
    quality: dict[str, dict[str, AircraftQuality]] = defaultdict(dict)
    providers: dict[str, set[str]] = defaultdict(set)
    for o in sorted(in_window(obs, start, end), key=lambda o: (o.ts, o.hex, o.provider)):
        if o.on_ground:
            continue
        cell = cell_of(o, c["h3_res"])
        seen[cell].add(o.hex)
        if not o.is_true_adsb:
            continue
        d = _degraded(o, c["nic_bad_below"], c["nacp_bad_below"])
        if d is None:
            continue
        providers[cell].add(o.provider)
        aq = quality[cell].setdefault(o.hex, AircraftQuality(o.hex))
        aq.n_known += 1
        pv = aq.by_provider.setdefault(o.provider, [0, 0])
        pv[0] += 1
        if d:
            aq.n_bad += 1
            pv[1] += 1
            aq.first_bad_by_provider.setdefault(o.provider, o)
            if aq.first_bad is None:
                aq.first_bad = o
            aq.last_bad = o
    out: dict[str, CellStats] = {}
    frac = c["bad_dwell_frac"]
    for cell, hexes in seen.items():
        acs = quality.get(cell, {})
        total = len(acs)
        bad_list = sorted((a for a in acs.values() if a.is_bad(frac)), key=lambda a: a.hex)
        bad = len(bad_list)
        ratio = max(0, bad - 1) / total if total else 0.0
        # agreement: for each bad aircraft, share of providers that also see it as bad
        agr = [
            sum(1 for k, b in a.by_provider.values() if k and b / k >= frac) / len(a.by_provider)
            for a in bad_list
        ]
        out[cell] = CellStats(
            cell=cell,
            n_observed=len(hexes),
            total=total,
            bad=bad,
            ratio=ratio,
            providers=sorted(providers.get(cell, set())),
            agreement=sum(agr) / len(agr) if agr else 1.0,
            bad_aircraft=bad_list,
        )
    return out


# --------------------------------------------------------------------------- localisation

_FT_M = 0.3048


@dataclass
class _Fix:
    lat: float
    lon: float
    alt_m: float  # NaN if unknown
    degraded: bool


def _loc_pool(
    obs: Sequence[AircraftObs], cfg: dict[str, Any], now: datetime
) -> dict[str, list[_Fix]]:
    """Airborne true-ADS-B fixes with known quality in the window, per hex, time-sorted."""
    c = _cfg(cfg)
    start, end = align_window(now, c["window_min"], c["step_min"])
    pool: dict[str, list[_Fix]] = defaultdict(list)
    for o in sorted(in_window(obs, start, end), key=lambda o: (o.ts, o.hex, o.provider)):
        if o.on_ground or not o.is_true_adsb:
            continue
        d = _degraded(o, c["nic_bad_below"], c["nacp_bad_below"])
        if d is None:
            continue
        alt = o.alt_geom if o.alt_geom is not None else o.alt_baro
        pool[o.hex].append(_Fix(o.lat, o.lon, math.nan if alt is None else alt * _FT_M, d))
    return pool


def wide_bad(pool: dict[str, list[_Fix]], frac: float) -> set[str]:
    """Hexes degraded on >= `frac` (bad_dwell_frac) of their window fixes (any cell)."""
    return {h for h, fx in pool.items() if sum(f.degraded for f in fx) >= frac * len(fx)}


def _thin(fixes: list[_Fix], n: int) -> list[_Fix]:
    if len(fixes) <= n:
        return fixes
    idx = set(np.linspace(0, len(fixes) - 1, n).round().astype(int).tolist())
    for i in range(1, len(fixes)):  # keep both sides of every degraded/healthy flip: they
        if fixes[i].degraded != fixes[i - 1].degraded:  # bracket the range boundary
            idx.update((i - 1, i))
    return [fixes[i] for i in sorted(idx)]


def _xyz(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    la, lo = np.radians(lat), np.radians(lon)
    return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], axis=1)


def _class_arrays(
    per_ac: list[list[_Fix]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(lat, lon, alt_m, weight, aircraft index); weight = 1/(n_aircraft * n_fixes of aircraft)."""
    lat, lon, alt, w, idx = [], [], [], [], []
    for i, fixes in enumerate(per_ac):
        for f in fixes:
            lat.append(f.lat)
            lon.append(f.lon)
            alt.append(f.alt_m)
            w.append(1.0 / (len(per_ac) * len(fixes)))
            idx.append(i)
    return np.array(lat), np.array(lon), np.array(alt), np.array(w), np.array(idx, dtype=int)


@dataclass
class Localisation:
    lat: float  # centroid of the near-optimal cell set
    lon: float
    region_pts: list[tuple[float, float]]  # top-k cell centres
    agreement: float  # balanced (degraded-inside, healthy-outside) agreement at the best cell
    degraded_inside: float
    healthy_outside: float
    n_degraded_ac: int
    n_healthy_ac: int
    n_candidates: int
    radius_km: float


def localise_emitter(
    pool: dict[str, list[_Fix]],
    bad_hexes: Collection[str],
    centroid: tuple[float, float],
    cfg: dict[str, Any],
) -> tuple[Localisation | None, str]:
    """Radio-horizon grid search. Returns (result, reason); result None => use the centroid.

    Model: a ground emitter at height h_j covers an aircraft at height h (m) out to
    d = K (sqrt(h) + sqrt(h_j)) km (K = 4.12). Candidates are H3 r6 cell centres within
    `loc_radius_km` of the degraded-fix centroid. Score = 0.5 (TPR + TNR) over per-aircraft
    weighted fixes (TPR: degraded fixes inside their range; TNR: healthy fixes, restricted to
    altitudes within `loc_alt_band_m` of a degraded fix, outside their range), plus a small
    soft-margin term that breaks plateaus.
    """
    c = _cfg(cfg)
    bad = set(bad_hexes)
    clat, clon = centroid
    cxyz = _xyz(np.array([clat]), np.array([clon]))[0]

    def dist_to_centroid(fx: list[_Fix]) -> np.ndarray:
        xyz = _xyz(np.array([f.lat for f in fx]), np.array([f.lon for f in fx]))
        return EARTH_RADIUS_KM * np.arccos(np.clip(xyz @ cxyz, -1.0, 1.0))

    deg_ac: list[list[_Fix]] = []
    for h, fx in sorted(pool.items()):
        if h not in bad:
            continue
        cand = [f for f in fx if f.degraded and not math.isnan(f.alt_m)]
        if cand:
            near = dist_to_centroid(cand) <= c["loc_degraded_radius_km"]
            cand = [f for f, ok in zip(cand, near, strict=True) if ok]
        if cand:
            deg_ac.append(_thin(cand, c["loc_max_fixes_per_ac"]))
    if len(deg_ac) < c["loc_min_degraded"]:
        return (
            None,
            f"only {len(deg_ac)} degraded aircraft with altitude (need {c['loc_min_degraded']})",
        )
    dlat, dlon, dalt, dw, _ = _class_arrays(deg_ac)
    # healthy fixes: aircraft never degraded in the window, plus the healthy fixes of bad
    # aircraft (before/after they cross the range boundary); nearest to the centroid first.
    # Aircraft degraded only part of the time (< bad_dwell_frac) are ambiguous and skipped.
    cand_h: list[tuple[float, list[_Fix]]] = []
    for h, fx in sorted(pool.items()):
        if h not in bad and any(f.degraded for f in fx):
            continue
        good = [f for f in fx if not f.degraded and not math.isnan(f.alt_m)]
        if not good:
            continue
        dist = dist_to_centroid(good)
        if dist.min() <= c["loc_healthy_radius_km"]:
            cand_h.append((float(dist.min()), good))
    cand_h.sort(key=lambda t: t[0])
    hea_ac = [_thin(g, c["loc_max_fixes_per_ac"]) for _, g in cand_h[: c["loc_max_healthy_ac"]]]
    if not hea_ac:
        return None, f"only 0 healthy aircraft with altitude (need {c['loc_min_healthy']})"
    hlat, hlon, halt, hw, hidx = _class_arrays(hea_ac)
    keep = np.flatnonzero(np.abs(halt[:, None] - dalt[None, :]).min(axis=1) <= c["loc_alt_band_m"])
    hlat, hlon, halt, hw, hidx = hlat[keep], hlon[keep], halt[keep], hw[keep], hidx[keep]
    n_hea = len(set(hidx.tolist()))
    if n_hea < c["loc_min_healthy"]:
        return (
            None,
            f"only {n_hea} healthy aircraft at similar altitude (need {c['loc_min_healthy']})",
        )
    hw = hw / hw.sum()

    # candidate grid (radius shrinks so the disk fits the cap)
    edge_km = 5.6  # r6 centre spacing (sqrt(3) x 3.23 km); only used to size k
    k = math.ceil(c["loc_radius_km"] / edge_km)
    while 1 + 3 * k * (k + 1) > c["loc_max_candidates"] and k > 1:
        k -= 1
    cells = h3_disk(h3_cell(clat, clon, c["loc_res"]), k)
    centers = np.array([h3_center(x) for x in cells])
    within = EARTH_RADIUS_KM * np.arccos(
        np.clip(_xyz(centers[:, 0], centers[:, 1]) @ cxyz, -1.0, 1.0)
    )
    centers = centers[within <= c["loc_radius_km"]]
    if len(centers) == 0:
        return None, "no candidate cells"
    kk, hj = c["loc_horizon_km"], math.sqrt(c["loc_jammer_height_m"])
    rng_d = kk * (np.sqrt(np.maximum(dalt, 0.0)) + hj)
    rng_h = kk * (np.sqrt(np.maximum(halt, 0.0)) + hj)
    dxyz, hxyz = _xyz(dlat, dlon), _xyz(hlat, hlon)
    tpr = np.empty(len(centers))
    tnr = np.empty(len(centers))
    total = np.empty(len(centers))
    sw = c["loc_soft_weight"]
    for i in range(0, len(centers), 2000):
        cx = _xyz(centers[i : i + 2000, 0], centers[i : i + 2000, 1])
        dd = EARTH_RADIUS_KM * np.arccos(np.clip(cx @ dxyz.T, -1.0, 1.0))
        dh = EARTH_RADIUS_KM * np.arccos(np.clip(cx @ hxyz.T, -1.0, 1.0))
        t = (dd <= rng_d) @ dw
        u = (dh > rng_h) @ hw
        soft = np.clip(1 - dd / rng_d, 0, 1) @ dw + np.clip(dh / rng_h - 1, 0, 1) @ hw
        tpr[i : i + 2000], tnr[i : i + 2000] = t, u
        total[i : i + 2000] = 0.5 * (t + u) + sw * 0.5 * soft
    bal = 0.5 * (tpr + tnr)
    best = int(np.argmax(total))
    # near-optimal set: balanced agreement within tol of the maximum (a score plateau is
    # the honest feasible region), never fewer than top_k cells by total score
    near = np.flatnonzero(bal >= bal.max() - c["loc_agreement_tol"])
    topk = np.argsort(-total)[: max(1, int(c["loc_top_k"]))]
    region = np.union1d(near, topk)
    rxyz = _xyz(centers[region, 0], centers[region, 1]).mean(axis=0)
    rxyz /= np.linalg.norm(rxyz)
    pt_lat = float(np.degrees(np.arcsin(np.clip(rxyz[2], -1.0, 1.0))))
    pt_lon = float(np.degrees(np.arctan2(rxyz[1], rxyz[0])))
    if len(region) > 400:  # keep the covariance cheap; even stride preserves the shape
        region = region[np.linspace(0, len(region) - 1, 400).astype(int)]
    return (
        Localisation(
            lat=pt_lat,
            lon=pt_lon,
            region_pts=[(float(centers[j, 0]), float(centers[j, 1])) for j in region],
            agreement=float(bal[best]),
            degraded_inside=float(tpr[best]),
            healthy_outside=float(tnr[best]),
            n_degraded_ac=len(deg_ac),
            n_healthy_ac=n_hea,
            n_candidates=len(centers),
            radius_km=float(c["loc_radius_km"]),
        ),
        "",
    )


def detect(
    obs: Sequence[AircraftObs],
    cfg: dict[str, Any],
    now: datetime,
    baseline: Baseline | None = None,
    *,
    active: Collection[str] | None = None,
    aoi: str | None = None,
) -> list[Evidence]:
    """Return one Evidence per H3 cell whose degraded ratio triggers (or is held by hysteresis).

    Trigger (cell not active):  total >= min_aircraft AND R' >= ratio_alert AND
                                (z >= robust_z  OR  baseline has < baseline_min_samples values).
    Hold (cell in `active`):    total >= min_aircraft AND R' >= ratio_clear.
    """
    c = _cfg(cfg)
    start, end = align_window(now, c["window_min"], c["step_min"])
    active_set = set(active or ())
    res: list[Evidence] = []
    pool: dict[str, list[_Fix]] | None = None  # built lazily, only if a cell fires
    for cell, st in sorted(cell_stats(obs, cfg, now).items()):
        if st.total == 0:
            continue  # MLAT-only / no quality data: cannot judge
        held = cell in active_set
        hist = baseline(cell, METRIC) if baseline else []
        has_baseline = len(hist) >= c["baseline_min_samples"]
        z, b_med, b_scale = (
            robust_z(st.ratio, hist, c["mad_floor"])
            if has_baseline
            else (
                None,
                None,
                None,
            )
        )
        thr = c["ratio_clear"] if held else c["ratio_alert"]
        r_min = RuleResult(
            rule="min_aircraft",
            description="Enough aircraft reporting GPS accuracy in the area",
            value=float(st.total),
            threshold=float(c["min_aircraft"]),
            passed=st.total >= c["min_aircraft"],
        )
        r_ratio = RuleResult(
            rule="degraded_ratio",
            description=(
                "Share of aircraft that lost GPS accuracy (one aircraft discounted as noise)"
                + (" - alert kept while above the lower clear level" if held else "")
            ),
            value=round(st.ratio, 4),
            threshold=thr,
            passed=st.ratio >= thr,
            baseline=None if b_med is None else round(b_med, 4),
        )
        z_ok = True if (held or not has_baseline) else (z is not None and z >= c["robust_z"])
        r_z = RuleResult(
            rule="robust_z",
            description=(
                "How unusual this share is compared with the last 14 days in this area"
                if has_baseline
                else "Not compared with history: too little baseline data"
            ),
            value=None if z is None else round(z, 3),
            threshold=c["robust_z"],
            passed=z_ok,
            baseline=None if b_med is None else round(b_med, 4),
        )
        rules = [r_min, r_ratio, r_z]
        if held:
            rules.append(
                RuleResult(
                    rule="hysteresis_hold",
                    description=(
                        "Alert already active; kept until the share drops below the clear level"
                    ),
                    value=1.0,
                    threshold=1.0,
                    op="==",
                    passed=True,
                )
            )
        if not (r_min.passed and r_ratio.passed and r_z.passed):
            continue

        pts = [(a.first_bad.lat, a.first_bad.lon) for a in st.bad_aircraft if a.first_bad]
        lat_c, lon_c, maj, mnr, ang, geom = ellipse_2sigma(pts)
        loc: Localisation | None = None
        loc_reason = "disabled"
        if c["loc_enabled"]:
            if pool is None:
                pool = _loc_pool(obs, cfg, now)
            loc, loc_reason = localise_emitter(
                pool,
                wide_bad(pool, c["bad_dwell_frac"]) | {a.hex for a in st.bad_aircraft},
                (lat_c, lon_c),
                cfg,
            )
            if loc is not None and loc.agreement < c["loc_min_agreement"]:
                loc_reason = (
                    f"radio-horizon fit agreement {loc.agreement:.0%} "
                    f"< {c['loc_min_agreement']:.0%}"
                )
                loc = None
        centroid_ll = (lat_c, lon_c)
        if loc is not None:
            lat_c, lon_c, maj, mnr, ang, geom = ellipse_2sigma(loc.region_pts)
            lat_c, lon_c = loc.lat, loc.lon  # best point; ellipse stays on the top-k centroid
        n_prov = len(st.providers)
        conf = min(1.0, st.total / c["full_coverage_aircraft"]) * st.agreement
        gaps: list[str] = []
        if n_prov <= 1:
            conf *= c["single_provider_factor"]
            gaps.append("single provider: no cross-aggregator confirmation")
        if conf > c["confidence_cap"]:
            conf = c["confidence_cap"]
            gaps.append(f"sparse-traffic profile: confidence capped at {c['confidence_cap']}")
        if not has_baseline:
            gaps.append(
                f"no baseline: only {len(hist)} of {c['baseline_min_samples']} history samples"
            )
        if st.n_observed > st.total:
            gaps.append(f"{st.n_observed - st.total} aircraft without GPS accuracy data left out")
        if loc is None:
            gaps.append(f"source estimated as centroid of first degraded fixes: {loc_reason}")
            if len(pts) < 3:
                gaps.append(
                    "source area estimated from fewer than 3 aircraft (5 km minimum radius)"
                )
        max_bad_quiet = math.floor(thr * st.total + 1 - 1e-9)
        cf = (
            f"Clears when {max_bad_quiet} or fewer of the {st.total} aircraft lose GPS accuracy "
            f"(alert level {thr:.0%}, now {st.ratio:.0%})"
        )
        if has_baseline and not held and b_med is not None and b_scale is not None:
            quiet_med = st.ratio - c["robust_z"] * b_scale
            cf += f", or if this area normally had {quiet_med:.0%} or more degraded"
        cf += "."
        if loc is not None:
            cf += (
                f" Source estimated by radio-horizon fit (jammer height "
                f"{c['loc_jammer_height_m']:g} m): {loc.degraded_inside:.0%} of degraded fixes "
                f"inside and {loc.healthy_outside:.0%} of healthy fixes outside line-of-sight "
                f"range "
                f"({loc.agreement:.0%} agreement; {loc.n_degraded_ac} degraded and "
                f"{loc.n_healthy_ac} healthy aircraft, {loc.n_candidates} candidate cells within "
                f"{loc.radius_km:g} km); the estimate is void if the emitter is not ground-based."
            )
        else:
            cf += f" Source estimated as centroid of first degraded fixes ({loc_reason})."
        features = {
            "total": float(st.total),
            "bad": float(st.bad),
            "n_observed": float(st.n_observed),
            "ratio": round(st.ratio, 4),
            "raw_ratio": round(st.bad / st.total, 4),
            "baseline_n": float(len(hist)),
            "provider_agreement": round(st.agreement, 3),
            "emitter_lat": round(lat_c, 4),
            "emitter_lon": round(lon_c, 4),
            "ellipse_major_km": round(maj, 2),
            "ellipse_minor_km": round(mnr, 2),
            "ellipse_angle_deg": round(ang, 1),
            # 1.0 = radio-horizon fit, 0.0 = centroid fallback (features are numeric)
            "emitter_method": 0.0 if loc is None else 1.0,
            "held": 1.0 if held else 0.0,
            # time span of degraded reports from bad aircraft (drives persistence, see pipeline)
            "anomaly_first_ts": min(
                a.first_bad.ts for a in st.bad_aircraft if a.first_bad
            ).timestamp(),
            "anomaly_last_ts": max(
                a.last_bad.ts for a in st.bad_aircraft if a.last_bad
            ).timestamp(),
        }
        if loc is not None:
            features.update(
                emitter_agreement=round(loc.agreement, 3),
                emitter_degraded_inside=round(loc.degraded_inside, 3),
                emitter_healthy_outside=round(loc.healthy_outside, 3),
                emitter_n_degraded=float(loc.n_degraded_ac),
                emitter_n_healthy=float(loc.n_healthy_ac),
                emitter_n_candidates=float(loc.n_candidates),
                emitter_jammer_height_m=float(c["loc_jammer_height_m"]),
                emitter_centroid_lat=round(centroid_ll[0], 4),
                emitter_centroid_lon=round(centroid_ll[1], 4),
            )
        if z is not None:
            features["robust_z"] = round(z, 3)
        res.append(
            Evidence(
                id=evidence_id(AlertKind.GNSS_JAMMING.value, cell, "", start, aoi),
                kind=AlertKind.GNSS_JAMMING,
                detector=NAME,
                bands=[Band.L1_GNSS],
                detector_version=VERSION,
                aoi=aoi,
                window_start=start,
                window_end=end,
                h3_cell=cell,
                h3_res=c["h3_res"],
                lat=lat_c,
                lon=lon_c,
                geometry=geom,
                score=round(min(1.0, st.ratio / (3 * c["ratio_alert"])), 4),
                rules=rules,
                features=features,
                # one record per (bad aircraft, provider whose own reports show it degraded)
                records=[
                    RecordRef(source=p, id=record_id(a.first_bad_by_provider[p]))
                    for a in st.bad_aircraft
                    for p in a.showing_providers(c["bad_dwell_frac"])
                ][:150],
                entities=[a.hex for a in st.bad_aircraft],
                counterfactual=cf,
                coverage=Coverage(
                    n_observed=st.n_observed,
                    n_quality=st.total,
                    providers=st.providers,
                    confidence=round(conf, 4),
                ),
                gaps=gaps,
                created_at=now,
            )
        )
    return res
