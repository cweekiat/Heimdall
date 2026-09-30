"""GNSS spoofing detector: per-aircraft kinematic/consistency signals S1..S5.

Assumptions
-----------
* Only true ADS-B fixes are self-reported GNSS positions; MLAT fixes (`src_type == 'mlat'`) are
  independent ground truth for S3 only.
* Identical (hex, ts) fixes from several aggregators are one fix; ties broken by provider name.
* Consecutive fixes closer than `min_dt_s` are thinned (anchor-based) to suppress timestamp
  quantisation noise. The |v_calc - gs| test is only applied to pairs with dt <= `speed_pair_max_s`
  because chords under-estimate speed in turns (a 90-degree turn in 30 s costs < 10 %).
* A single out-and-back outlier fix (both adjacent pairs flagged, the bridging pair clean) is a
  *glitch* and removed before S1 is evaluated.
* Spoofing usually leaves NIC healthy (the receiver trusts the fake solution); faulty avionics
  and jamming degrade NIC. S5 captures this.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from heimdall.detect.common import (
    Baseline,
    align_window,
    cell_of,
    cfg_get,
    evidence_id,
    in_window,
    median,
    record_id,
)
from heimdall.geo import KM_PER_NM, circular_variance, haversine_km, implied_speed_kt
from heimdall.schemas import AircraftObs, AlertKind, Coverage, Evidence, RecordRef, RuleResult

NAME = "spoofing"
VERSION = "spoofing-1.0.0"

DEFAULTS: dict[str, Any] = {
    "h3_res": 4,
    "window_min": 30,
    "step_min": 5,
    "min_dt_s": 5,
    "speed_pair_max_s": 30,
    "teleport_kt": 1323,
    "speed_diff_kt": 150,
    "persist_pairs": 2,
    "s1_span_cap_s": 300,
    "alt_divergence_ft": 1000,
    "alt_min_fixes": 2,
    "alt_min_frac": 0.5,
    "alt_norm_min_aircraft": 3,
    "mlat_gap_km": 5,
    "mlat_max_dt_s": 10,
    "mlat_min_pairs": 2,
    "cluster_km": 2.0,
    "cluster_min_aircraft": 3,
    "cluster_bin_s": 60,
    "cluster_circvar_min": 0.3,
    "cluster_min_bins": 2,
    "nic_trusted_min": 7,
    "min_signals": 2,
    "full_coverage_fixes": 20,
    "single_provider_factor": 0.8,
}

SIGNALS = ("S1", "S2", "S3", "S4", "S5")


@dataclass
class AircraftSignals:
    hex: str
    fired: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(SIGNALS, False))
    features: dict[str, float] = field(default_factory=dict)
    anchor: AircraftObs | None = None  # last good fix before the anomaly (localises the event)
    displaced: AircraftObs | None = None  # first anomalous fix
    records: list[AircraftObs] = field(default_factory=list)
    anomalous: list[AircraftObs] = field(default_factory=list)  # fixes of fired signals
    raw: list[AircraftObs] = field(default_factory=list)  # all providers' fixes of this hex
    n_fixes: int = 0
    providers: set[str] = field(default_factory=set)

    @property
    def n_signals(self) -> int:
        return sum(self.fired.values())


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def _dedupe(fixes: Sequence[AircraftObs]) -> list[AircraftObs]:
    out: dict[datetime, AircraftObs] = {}
    for o in sorted(fixes, key=lambda o: (o.ts, o.provider)):
        out.setdefault(o.ts, o)
    return list(out.values())


def _thin(fixes: Sequence[AircraftObs], min_dt_s: float) -> list[AircraftObs]:
    out: list[AircraftObs] = []
    for o in fixes:
        if not out or (o.ts - out[-1].ts).total_seconds() >= min_dt_s:
            out.append(o)
    return out


def _pair(a: AircraftObs, b: AircraftObs, c: dict[str, Any]) -> tuple[bool, float]:
    """(flagged, v_calc_kt) for a pair of fixes."""
    t1, t2 = a.ts.timestamp(), b.ts.timestamp()
    v = implied_speed_kt(a.lat, a.lon, t1, b.lat, b.lon, t2)
    if v > c["teleport_kt"]:
        return True, v
    if (t2 - t1) <= c["speed_pair_max_s"] and a.gs is not None and b.gs is not None:
        gs = (a.gs + b.gs) / 2
        if abs(v - gs) > c["speed_diff_kt"]:
            return True, v
    return False, v


def _s1(fixes: list[AircraftObs], c: dict[str, Any], sig: AircraftSignals) -> None:
    seq = _thin(fixes, c["min_dt_s"])
    if len(seq) < 2:
        return
    flags = [_pair(a, b, c)[0] for a, b in itertools.pairwise(seq)]
    glitch = set()
    for i in range(1, len(seq) - 1):
        if flags[i - 1] and flags[i] and not _pair(seq[i - 1], seq[i + 1], c)[0]:
            glitch.add(i)
    clean = [o for i, o in enumerate(seq) if i not in glitch]
    pairs = [(a, b, *_pair(a, b, c)) for a, b in itertools.pairwise(clean)]
    flagged = [k for k, p in enumerate(pairs) if p[2]]
    sig.features["n_glitches"] = float(len(glitch))
    sig.features["n_flagged_pairs"] = float(len(flagged))
    sig.features["max_v_calc_kt"] = round(max((p[3] for p in pairs), default=0.0), 1)
    if not flagged:
        return
    k0 = flagged[0]
    trailing = len(pairs) - 1 - flagged[-1]  # clean pairs after the last jump
    persisted = len(flagged) + (trailing if len(flagged) == 1 else 0)
    sig.features["s1_persisted_pairs"] = float(persisted)
    a, b = pairs[k0][0], pairs[k0][1]
    sig.features["jump_km"] = round(haversine_km(a.lat, a.lon, b.lat, b.lon), 2)
    if persisted >= c["persist_pairs"]:
        sig.fired["S1"] = True
        sig.anchor, sig.displaced = a, b
        sig.records += [a, b]
        # anomalous span of each jump: the displaced segment until the track re-converges (next
        # flagged pair) and strictly shorter than `s1_span_cap_s`, so one jump alone can never
        # span a full persistence step
        cap = timedelta(seconds=c["s1_span_cap_s"])
        # flags alternate: odd flags open a displaced segment, the next flag closes it (jump back)
        for i in range(0, len(flagged), 2):
            k = flagged[i]
            stop = flagged[i + 1] + 1 if i + 1 < len(flagged) else len(clean)
            t_jump = clean[k + 1].ts
            sig.anomalous += [o for o in clean[k + 1 : stop] if o.ts - t_jump < cap]
        after = [o.nic for o in clean[k0 + 1 :] if o.nic is not None]
        if after:
            sig.features["nic_median_anomalous"] = median([float(n) for n in after])


def _s3(
    adsb: list[AircraftObs], mlat: list[AircraftObs], c: dict[str, Any], sig: AircraftSignals
) -> None:
    if not adsb or not mlat:
        return
    gaps, bad = [], []
    for m in mlat:
        best = min(adsb, key=lambda a: abs((a.ts - m.ts).total_seconds()))
        dt = abs((best.ts - m.ts).total_seconds())
        if dt > c["mlat_max_dt_s"]:
            continue
        gap = haversine_km(best.lat, best.lon, m.lat, m.lon)
        allow = c["mlat_gap_km"] + (best.gs or 0.0) * KM_PER_NM * dt / 3600.0
        gaps.append(gap)
        if gap > allow:
            bad.append((best, m))
    if not gaps:
        return
    sig.features["mlat_pairs"] = float(len(gaps))
    sig.features["mlat_gap_km_median"] = round(median(gaps), 2)
    sig.features["mlat_bad_pairs"] = float(len(bad))
    if len(bad) >= c["mlat_min_pairs"]:
        sig.fired["S3"] = True
        if sig.anchor is None:
            sig.anchor, sig.displaced = bad[0][1], bad[0][0]  # MLAT = truth, ADS-B = claimed
        sig.records += [bad[0][0], bad[0][1]]
        sig.anomalous += [x for pair in bad for x in pair]
        nics = [float(a.nic) for a, _ in bad if a.nic is not None]
        if nics and "nic_median_anomalous" not in sig.features:
            sig.features["nic_median_anomalous"] = median(nics)


def _s4_bins(
    adsb_by_hex: dict[str, list[AircraftObs]], c: dict[str, Any]
) -> dict[str, list[AircraftObs]]:
    """Count time bins in which each aircraft sits in a >=N-aircraft, <=cluster_km cluster whose
    reported tracks disagree (circular variance >= cluster_circvar_min)."""
    bins: dict[int, dict[str, list[AircraftObs]]] = defaultdict(lambda: defaultdict(list))
    for h, fixes in adsb_by_hex.items():
        for o in fixes:
            bins[int(o.ts.timestamp() // c["cluster_bin_s"])][h].append(o)
    counts: dict[str, list[AircraftObs]] = defaultdict(list)
    g = c["cluster_km"] / 111.0  # grid size in degrees lat (conservative for lon)
    for per_hex in bins.values():
        pos = {
            h: (
                sum(o.lat for o in fs) / len(fs),
                sum(o.lon for o in fs) / len(fs),
                [o.track for o in fs if o.track is not None],
            )
            for h, fs in per_hex.items()
        }
        grid: dict[tuple[int, int], list[str]] = defaultdict(list)
        for h, (la, lo, _) in pos.items():
            grid[(math.floor(la / g), math.floor(lo / g))].append(h)
        hit: set[str] = set()
        for la, lo, _ in pos.values():
            gi, gj = math.floor(la / g), math.floor(lo / g)
            near = [
                k
                for di in (-1, 0, 1)
                for dj in (-2, -1, 0, 1, 2)
                for k in grid.get((gi + di, gj + dj), [])
                if haversine_km(la, lo, pos[k][0], pos[k][1]) <= c["cluster_km"]
            ]
            if len(near) < c["cluster_min_aircraft"]:
                continue
            tracks = [median(pos[k][2]) for k in near if pos[k][2]]
            if len(tracks) >= c["cluster_min_aircraft"] and (
                circular_variance(tracks) >= c["cluster_circvar_min"]
            ):
                hit.update(near)
        for h in hit:
            counts[h].append(per_hex[h][0])
    return counts


def showing_records(
    sig: AircraftSignals, tol_s: float = 2.0, tol_km: float = 1.0
) -> list[AircraftObs]:
    """First fix per provider whose *own* report reproduces an anomalous fix (same hex,
    |dt| <= tol_s, <= tol_km). Only these providers count toward Admiralty reliability."""
    out: dict[str, AircraftObs] = {}
    for a in sorted(sig.anomalous, key=lambda o: o.ts):
        for o in sig.raw:
            if o.provider in out or o.src_type != a.src_type:
                continue
            if abs((o.ts - a.ts).total_seconds()) <= tol_s and (
                haversine_km(o.lat, o.lon, a.lat, a.lon) <= tol_km
            ):
                out[o.provider] = o
    return sorted(out.values(), key=lambda o: o.provider)


def aircraft_signals(
    obs: Sequence[AircraftObs], cfg: dict[str, Any], now: datetime
) -> dict[str, AircraftSignals]:
    """Evaluate S1..S5 for every aircraft in the aligned window. Pure; exposed for tests."""
    c = _cfg(cfg)
    start, end = align_window(now, c["window_min"], c["step_min"])
    adsb: dict[str, list[AircraftObs]] = defaultdict(list)
    mlat: dict[str, list[AircraftObs]] = defaultdict(list)
    for o in in_window(obs, start, end):
        if o.on_ground:
            continue
        if o.is_true_adsb:
            adsb[o.hex].append(o)
        elif o.src_type == "mlat":
            mlat[o.hex].append(o)
    adsb_d = {h: _dedupe(fs) for h, fs in adsb.items()}
    mlat_d = {h: _dedupe(fs) for h, fs in mlat.items()}

    # S2 regional norm: median over aircraft (per cell) of each aircraft's median geom-baro
    per_ac_div: dict[str, list[float]] = {}
    cell_divs: dict[str, list[float]] = defaultdict(list)
    for h, fs in adsb_d.items():
        d = [
            float(o.alt_geom - o.alt_baro)
            for o in fs
            if o.alt_geom is not None and o.alt_baro is not None
        ]
        if d:
            per_ac_div[h] = d
            cell_divs[cell_of(fs[0], c["h3_res"])].append(median(d))
    s4 = _s4_bins(adsb_d, c)

    out: dict[str, AircraftSignals] = {}
    for h, fs in adsb_d.items():
        sig = AircraftSignals(h, n_fixes=len(fs), providers={o.provider for o in adsb[h]})
        sig.raw = [*adsb[h], *mlat.get(h, [])]
        _s1(fs, c, sig)
        if h in per_ac_div:
            norms = cell_divs[cell_of(fs[0], c["h3_res"])]
            has_norm = len(norms) >= c["alt_norm_min_aircraft"]
            norm = median(norms) if has_norm else 0.0
            dev = [abs(d - norm) for d in per_ac_div[h]]
            n_over = sum(1 for x in dev if x > c["alt_divergence_ft"])
            sig.features["alt_div_ft_median"] = round(median(dev), 1)
            sig.features["alt_norm_ft"] = round(norm, 1)
            sig.features["alt_norm_available"] = 1.0 if has_norm else 0.0
            if n_over >= c["alt_min_fixes"] and n_over / len(dev) >= c["alt_min_frac"]:
                sig.fired["S2"] = True
                sig.anomalous += [
                    o
                    for o in fs
                    if o.alt_geom is not None
                    and o.alt_baro is not None
                    and abs(o.alt_geom - o.alt_baro - norm) > c["alt_divergence_ft"]
                ]
                if sig.anchor is None:
                    first = next(
                        o
                        for o in fs
                        if o.alt_geom is not None
                        and o.alt_baro is not None
                        and abs(o.alt_geom - o.alt_baro - norm) > c["alt_divergence_ft"]
                    )
                    sig.anchor = first
                    sig.records.append(first)
        _s3(fs, mlat_d.get(h, []), c, sig)
        sig.features["cluster_bins"] = float(len(s4.get(h, [])))
        if len(s4.get(h, [])) >= c["cluster_min_bins"]:
            sig.fired["S4"] = True
            sig.anomalous += s4[h]
            if sig.anchor is None:
                sig.anchor = fs[0]
                sig.records.append(fs[0])
        nic_med = sig.features.get("nic_median_anomalous")
        if (sig.fired["S1"] or sig.fired["S3"]) and nic_med is not None:
            sig.fired["S5"] = nic_med >= c["nic_trusted_min"]
        out[h] = sig
    return out


def detect(
    obs: Sequence[AircraftObs],
    cfg: dict[str, Any],
    now: datetime,
    baseline: Baseline | None = None,
    *,
    aoi: str | None = None,
) -> list[Evidence]:
    """One Evidence per aircraft with >= min_signals signals, or with S3 (MLAT gap) alone."""
    c = _cfg(cfg)
    start, end = align_window(now, c["window_min"], c["step_min"])
    sigs = aircraft_signals(obs, cfg, now)
    # cell-level coverage for context
    cell_ac: dict[str, set[str]] = defaultdict(set)
    cell_q: dict[str, set[str]] = defaultdict(set)
    for o in in_window(obs, start, end):
        if o.on_ground:
            continue
        cl = cell_of(o, c["h3_res"])
        cell_ac[cl].add(o.hex)
        if o.is_true_adsb:
            cell_q[cl].add(o.hex)
    evs: list[Evidence] = []
    for h, s in sorted(sigs.items()):
        n = s.n_signals
        if not (n >= c["min_signals"] or s.fired["S3"]) or s.anchor is None:
            continue
        a = s.anchor
        cell = cell_of(a, c["h3_res"])
        f = s.features
        rules = [
            RuleResult(
                rule="S1_teleport",
                description=(
                    f"Implied speed > {c['teleport_kt']} kt or |v_calc-gs| > "
                    f"{c['speed_diff_kt']} kt persisting >= {c['persist_pairs']} pairs"
                ),
                value=f.get("s1_persisted_pairs", 0.0),
                threshold=float(c["persist_pairs"]),
                passed=s.fired["S1"],
            ),
            RuleResult(
                rule="S2_alt_divergence",
                description="Median |alt_geom-alt_baro - cell norm| (ft)",
                value=f.get("alt_div_ft_median"),
                threshold=float(c["alt_divergence_ft"]),
                op=">",
                passed=s.fired["S2"],
                baseline=f.get("alt_norm_ft"),
            ),
            RuleResult(
                rule="S3_mlat_gap",
                description=f"MLAT vs ADS-B pairs with gap > {c['mlat_gap_km']} km (+gs*dt)",
                value=f.get("mlat_bad_pairs", 0.0),
                threshold=float(c["mlat_min_pairs"]),
                passed=s.fired["S3"],
            ),
            RuleResult(
                rule="S4_cluster",
                description=(
                    f">= {c['cluster_min_aircraft']} aircraft within {c['cluster_km']} km with "
                    "disagreeing tracks (time bins)"
                ),
                value=f.get("cluster_bins", 0.0),
                threshold=float(c["cluster_min_bins"]),
                passed=s.fired["S4"],
            ),
            RuleResult(
                rule="S5_nic_trusted",
                description="Median NIC of anomalous fixes (healthy NIC while S1/S3 fire)",
                value=f.get("nic_median_anomalous"),
                threshold=float(c["nic_trusted_min"]),
                passed=s.fired["S5"],
            ),
            RuleResult(
                rule="signals",
                description="Independent spoofing signals fired (S3 alone suffices)",
                value=float(n),
                threshold=float(c["min_signals"]),
                passed=True,
            ),
        ]
        fired = [k for k in SIGNALS if s.fired[k]]
        gaps: list[str] = []
        if f.get("alt_norm_available", 1.0) == 0.0:
            gaps.append("no regional altitude norm (< 3 aircraft); S2 vs 0 ft")
        if "mlat_pairs" not in f:
            gaps.append("no MLAT cross-check for this aircraft")
        conf = min(1.0, s.n_fixes / c["full_coverage_fixes"])
        if len(s.providers) <= 1:
            conf *= c["single_provider_factor"]
            gaps.append("single provider")
        geom: dict[str, Any] | None = None
        if s.displaced is not None:
            d = s.displaced
            geom = {"type": "LineString", "coordinates": [[a.lon, a.lat], [d.lon, d.lat]]}
        feats = dict(f)
        feats.update({k: 1.0 if s.fired[k] else 0.0 for k in SIGNALS})
        feats["n_signals"] = float(n)
        if s.anomalous:
            feats["anomaly_first_ts"] = min(o.ts for o in s.anomalous).timestamp()
            feats["anomaly_last_ts"] = max(o.ts for o in s.anomalous).timestamp()
        cf = (
            f"Fired {', '.join(fired)}. Would not fire with < {c['min_signals']} signals "
            "and no MLAT gap"
        )
        cf += (
            f"; e.g. if the {f['jump_km']} km jump had reverted within one fix (glitch)."
            if "jump_km" in f and s.fired["S1"]
            else "."
        )
        shown = showing_records(s)
        show_prov = {o.provider for o in shown}
        # context records (S1 pre-jump anchor, S3 MLAT truth fix) kept when their provider also
        # shows the anomaly; de-duplicated by record id
        recs = list(
            {
                record_id(o) + o.provider: o
                for o in [*shown, *(o for o in s.records if o.provider in show_prov)]
            }.values()
        )[:40]
        evs.append(
            Evidence(
                id=evidence_id(AlertKind.GNSS_SPOOFING.value, cell, h, start, aoi),
                kind=AlertKind.GNSS_SPOOFING,
                detector=NAME,
                detector_version=VERSION,
                aoi=aoi,
                window_start=start,
                window_end=end,
                h3_cell=cell,
                h3_res=c["h3_res"],
                lat=a.lat,
                lon=a.lon,
                geometry=geom,
                score=round(max(0.5 if s.fired["S3"] else 0.0, min(1.0, n / 4)), 4),
                rules=rules,
                features=feats,
                records=[RecordRef(source=o.provider, id=record_id(o)) for o in recs],
                entities=[h],
                counterfactual=cf,
                coverage=Coverage(
                    n_observed=len(cell_ac.get(cell, ())) or 1,
                    n_quality=len(cell_q.get(cell, ())) or 1,
                    providers=sorted(s.providers),
                    confidence=round(conf, 4),
                ),
                gaps=gaps,
                created_at=now,
            )
        )
    return evs
