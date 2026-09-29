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
from heimdall.schemas import AircraftObs, AlertKind, Coverage, Evidence, RecordRef, RuleResult

NAME = "jamming"
VERSION = "jamming-1.0.0"
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
}


@dataclass
class AircraftQuality:
    hex: str
    n_known: int = 0
    n_bad: int = 0
    first_bad: AircraftObs | None = None
    by_provider: dict[str, list[int]] = field(default_factory=dict)  # provider -> [known, bad]

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
            if aq.first_bad is None:
                aq.first_bad = o
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


def detect(
    obs: Sequence[AircraftObs],
    cfg: dict[str, Any],
    now: datetime,
    baseline: Baseline | None = None,
    *,
    active: Collection[str] | None = None,
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
            description="Enough true-ADS-B aircraft with NIC/NACp in the cell",
            value=float(st.total),
            threshold=float(c["min_aircraft"]),
            passed=st.total >= c["min_aircraft"],
        )
        r_ratio = RuleResult(
            rule="degraded_ratio",
            description=(
                "De-noised share of aircraft with degraded GNSS, (bad-1)/total"
                + (" (hysteresis hold threshold)" if held else "")
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
                "Robust z of ratio vs 14-day median/MAD"
                if has_baseline
                else "Robust z skipped: insufficient baseline"
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
                    description="Cell was active last step; held while ratio >= ratio_clear",
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
        n_prov = len(st.providers)
        conf = min(1.0, st.total / c["full_coverage_aircraft"]) * st.agreement
        gaps: list[str] = []
        if n_prov <= 1:
            conf *= c["single_provider_factor"]
            gaps.append("single provider: no cross-aggregator confirmation")
        if not has_baseline:
            gaps.append(f"no baseline: {len(hist)} < {c['baseline_min_samples']} samples")
        if st.n_observed > st.total:
            gaps.append(
                f"{st.n_observed - st.total} aircraft without true-ADS-B quality data excluded"
            )
        if len(pts) < 3:
            gaps.append("emitter ellipse from < 3 aircraft (floored at 5 km sigma)")
        max_bad_quiet = math.floor(thr * st.total + 1 - 1e-9)
        cf = (
            f"Would not fire if <= {max_bad_quiet} of {st.total} aircraft were degraded "
            f"(R' < {thr})"
        )
        if has_baseline and not held and b_med is not None and b_scale is not None:
            quiet_med = st.ratio - c["robust_z"] * b_scale
            cf += f", or if the cell's baseline median were >= {quiet_med:.3f}"
        cf += "."
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
            "held": 1.0 if held else 0.0,
        }
        if z is not None:
            features["robust_z"] = round(z, 3)
        res.append(
            Evidence(
                id=evidence_id(AlertKind.GNSS_JAMMING.value, cell, "", start),
                kind=AlertKind.GNSS_JAMMING,
                detector=NAME,
                detector_version=VERSION,
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
                records=[
                    RecordRef(source=a.first_bad.provider, id=record_id(a.first_bad))
                    for a in st.bad_aircraft[:50]
                    if a.first_bad
                ],
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
