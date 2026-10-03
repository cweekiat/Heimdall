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
from heimdall.schemas import AircraftObs, AlertKind, Band, Coverage, Evidence, RecordRef, RuleResult

NAME = "spoofing"
VERSION = "spoofing-1.2.0"

DEFAULTS: dict[str, Any] = {
    "h3_res": 4,
    "window_min": 30,
    "step_min": 5,
    "min_dt_s": 5,
    "speed_pair_max_s": 30,
    "teleport_kt": 1323,
    "speed_diff_kt": 150,
    # Aggregator timestamps (now - seen_pos) wobble by a few seconds where feeders are sparse and
    # forward with delay (measured around Singapore 2026-09-30: 1,201 of 2,611 pairs < 5 s apart
    # jumped > 0.3 km). The speed test tolerates that much timing error.
    "timing_jitter_s": 6.0,
    # S5 (healthy NIC) is the normal state of an airliner: it only counts as the second signal
    # next to a true teleport (> teleport_kt) or an MLAT gap, not next to a speed mismatch alone.
    "s5_counts_as_signal": False,
    "persist_pairs": 2,
    # 041: a speed mismatch counts only if the position is really off (|distance - gs*dt|), and a
    # displaced position must last (a CPR/aggregator glitch snaps back within seconds)
    "min_error_km": 2.0,
    "min_displaced_s": 60,
    "s1_span_cap_s": 300,
    "alt_divergence_ft": 1000,
    "alt_min_fixes": 2,
    "alt_min_frac": 0.5,
    "alt_norm_min_aircraft": 3,
    "alt_norm_min_ft": 10000,  # 041: the per-foot norm is learned from aircraft above this
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
_SIGNAL_WORDS = {
    "S1": "position jump",
    "S2": "altitude mismatch",
    "S3": "radio-timing (MLAT) disagreement",
    "S4": "cluster of disagreeing tracks",
    "S5": "good GPS accuracy reported despite wrong positions",
}


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

    def n_independent(self, s5_counts: bool) -> int:
        """Signals counted towards `min_signals`; S5 needs a strong partner unless configured."""
        strong = self.features.get("s1_teleport", 0.0) > 0 or self.fired.get("S3", False)
        return sum(v for k, v in self.fired.items() if k != "S5" or s5_counts or strong)


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def _dedupe(fixes: Sequence[AircraftObs]) -> list[AircraftObs]:
    """One provider's track per aircraft (the one with most fixes; ties by name).

    041: interleaving adsb.lol and adsb.fi fixes mixes two timing bases (each stamps positions as
    fetch time - seen_pos) and creates apparent jumps of a few km: 56 % of Singapore-area S1 hits on
    2026-10-01 vanished when tracks were kept per provider."""
    by: dict[str, int] = defaultdict(int)
    for o in fixes:
        by[o.provider] += 1
    if not by:
        return []
    best = min(by, key=lambda p: (-by[p], p))
    out: dict[datetime, AircraftObs] = {}
    for o in sorted(fixes, key=lambda o: o.ts):
        if o.provider == best:
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
        tol = max(c["speed_diff_kt"], gs * c["timing_jitter_s"] / max(t2 - t1, 1e-6))
        err_km = abs(v - gs) * KM_PER_NM * (t2 - t1) / 3600.0  # how far the position is off
        if abs(v - gs) > tol and err_km >= c["min_error_km"]:
            return True, v
    return False, v


def _reconverges(
    anchor: AircraftObs, before: AircraftObs, after: AircraftObs, c: dict[str, Any]
) -> bool:
    """True if the jump before -> after returns to the track that `anchor` was on."""
    if _pair(anchor, after, c)[0]:
        return False
    d_after = haversine_km(anchor.lat, anchor.lon, after.lat, after.lon)
    return d_after < haversine_km(anchor.lat, anchor.lon, before.lat, before.lon)


def _displaced_segments(
    clean: Sequence[AircraftObs], flagged: Sequence[int], c: dict[str, Any]
) -> list[tuple[int, int]]:
    """Displaced segments as (k_open, stop): fixes clean[k_open + 1 : stop] are displaced.

    Flagged pairs are *not* assumed to alternate out/back. A flagged pair k closes the open
    segment only if its far fix clean[k+1] re-converges with the anchor (the last good fix
    before the *first* jump of the capture): the pair (anchor, clean[k+1]) passes the same pair
    rule **and** clean[k+1] is closer to the anchor than clean[k] is (the jump points back).
    Otherwise it is a further jump (displaced -> displaced, e.g. a spoofer stepping its fake
    position): the current segment closes at k and a new one starts at k, so each jump carries
    its own span cap, while re-convergence is still judged against the original anchor. A
    segment still open at the end of the window runs to the last fix.
    Limitation: if the window starts mid-capture, the first flagged pair is the jump *back* to
    the truth and is treated as an opening jump (no pre-window context to tell them apart).
    """
    segs: list[tuple[int, int]] = []
    anchor: int | None = None  # last good fix before the first jump of the current capture
    k_open: int | None = None  # start of the current displaced segment
    for k in flagged:
        if anchor is not None and k_open is not None:
            if _reconverges(clean[anchor], clean[k], clean[k + 1], c):
                segs.append((k_open, k + 1))  # back on the pre-jump track
                anchor = k_open = None
                continue
            segs.append((k_open, k + 1))  # further jump: close at k, re-open from k
        else:
            anchor = k
        k_open = k
    if k_open is not None:
        segs.append((k_open, len(clean)))
    return segs


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
    # 1 when a flagged pair is a true teleport (> teleport_kt), not only a speed mismatch
    sig.features["s1_teleport"] = float(any(pairs[k][3] > c["teleport_kt"] for k in flagged))
    if not flagged:
        return
    k0 = flagged[0]
    trailing = len(pairs) - 1 - flagged[-1]  # clean pairs after the last jump
    persisted = len(flagged) + (trailing if len(flagged) == 1 else 0)
    sig.features["s1_persisted_pairs"] = float(persisted)
    a, b = pairs[k0][0], pairs[k0][1]
    sig.features["jump_km"] = round(haversine_km(a.lat, a.lon, b.lat, b.lon), 2)
    # 041: the displaced position must last: from the first jump to the last flagged fix (or the
    # end of the window when the track never comes back) at least min_displaced_s
    t_first = clean[k0 + 1].ts
    t_last = clean[flagged[-1] + 1].ts if len(flagged) > 1 else clean[-1].ts
    lasted = (t_last - t_first).total_seconds()
    sig.features["displaced_s"] = round(lasted, 1)
    if persisted >= c["persist_pairs"] and lasted >= c["min_displaced_s"]:
        sig.fired["S1"] = True
        sig.anchor, sig.displaced = a, b
        sig.records += [a, b]
        # anomalous span of each jump: the displaced segment until the track re-converges (next
        # flagged pair) and strictly shorter than `s1_span_cap_s`, so one jump alone can never
        # span a full persistence step
        cap = timedelta(seconds=c["s1_span_cap_s"])
        for k, stop in _displaced_segments(clean, flagged, c):
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

    # S2 regional norm (041): geom - baro grows with altitude (temperature vs standard
    # atmosphere: ~+2,000 ft at cruise in the tropics), so the norm is a per-foot rate k learned
    # from aircraft above alt_norm_min_ft in the cell; expected divergence = k * alt_baro
    per_ac_div: dict[str, list[tuple[float, float]]] = {}  # (geom - baro, baro)
    cell_rates: dict[str, list[float]] = defaultdict(list)
    for h, fs in adsb_d.items():
        d = [
            (float(o.alt_geom - o.alt_baro), float(o.alt_baro))
            for o in fs
            if o.alt_geom is not None and o.alt_baro is not None
        ]
        if d:
            per_ac_div[h] = d
            high = [div / alt for div, alt in d if alt >= c["alt_norm_min_ft"]]
            if high:
                cell_rates[cell_of(fs[0], c["h3_res"])].append(median(high))
    s4 = _s4_bins(adsb_d, c)

    out: dict[str, AircraftSignals] = {}
    for h, fs in adsb_d.items():
        sig = AircraftSignals(h, n_fixes=len(fs), providers={o.provider for o in adsb[h]})
        sig.raw = [*adsb[h], *mlat.get(h, [])]
        _s1(fs, c, sig)
        if h in per_ac_div:
            rates = cell_rates[cell_of(fs[0], c["h3_res"])]
            has_norm = len(rates) >= c["alt_norm_min_aircraft"]
            k = median(rates) if has_norm else 0.0
            dev = [abs(d - k * alt) for d, alt in per_ac_div[h]]
            n_over = sum(1 for x in dev if x > c["alt_divergence_ft"])
            sig.features["alt_div_ft_median"] = round(median(dev), 1)
            sig.features["alt_norm_ft"] = round(k * median([alt for _, alt in per_ac_div[h]]), 1)
            sig.features["alt_norm_available"] = 1.0 if has_norm else 0.0

            def off(o: AircraftObs, k: float = k) -> bool:
                return (
                    o.alt_geom is not None
                    and o.alt_baro is not None
                    and abs(o.alt_geom - o.alt_baro - k * o.alt_baro) > c["alt_divergence_ft"]
                )

            # no norm, no verdict: comparing with 0 ft flagged every cruising airliner (041)
            if has_norm and n_over >= c["alt_min_fixes"] and n_over / len(dev) >= c["alt_min_frac"]:
                sig.fired["S2"] = True
                sig.anomalous += [o for o in fs if off(o)]
                if sig.anchor is None:
                    first = next(o for o in fs if off(o))
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
        n = s.n_independent(bool(c["s5_counts_as_signal"]))
        if not (n >= c["min_signals"] or s.fired["S3"]) or s.anchor is None:
            continue
        a = s.anchor
        cell = cell_of(a, c["h3_res"])
        f = s.features
        rules = [
            RuleResult(
                rule="S1_teleport",
                description=(
                    f"Position jumps faster than {c['teleport_kt']} kt, or disagrees with the "
                    f"reported ground speed by more than {c['speed_diff_kt']} kt, for at least "
                    f"{c['persist_pairs']} consecutive reports"
                ),
                value=f.get("s1_persisted_pairs", 0.0),
                threshold=float(c["persist_pairs"]),
                passed=s.fired["S1"],
            ),
            RuleResult(
                rule="S2_alt_divergence",
                description="GPS vs barometric altitude mismatch beyond the local norm (ft)",
                value=f.get("alt_div_ft_median"),
                threshold=float(c["alt_divergence_ft"]),
                op=">",
                passed=s.fired["S2"],
                baseline=f.get("alt_norm_ft"),
            ),
            RuleResult(
                rule="S3_mlat_gap",
                description=(
                    "Radio-timing (MLAT) position and reported position disagree by more than "
                    f"{c['mlat_gap_km']} km (plus distance flown between reports)"
                ),
                value=f.get("mlat_bad_pairs", 0.0),
                threshold=float(c["mlat_min_pairs"]),
                passed=s.fired["S3"],
            ),
            RuleResult(
                rule="S4_cluster",
                description=(
                    f"At least {c['cluster_min_aircraft']} aircraft within {c['cluster_km']} km "
                    "whose tracks disagree with each other"
                ),
                value=f.get("cluster_bins", 0.0),
                threshold=float(c["cluster_min_bins"]),
                passed=s.fired["S4"],
            ),
            RuleResult(
                rule="S5_nic_trusted",
                description="Aircraft still report good GPS accuracy while positions are wrong",
                value=f.get("nic_median_anomalous"),
                threshold=float(c["nic_trusted_min"]),
                passed=s.fired["S5"],
            ),
            RuleResult(
                rule="signals",
                description="Independent spoofing signals seen (MLAT disagreement alone suffices)",
                value=float(n),
                threshold=float(c["min_signals"]),
                passed=True,
            ),
        ]
        fired = [k for k in SIGNALS if s.fired[k]]
        gaps: list[str] = []
        if f.get("alt_norm_available", 1.0) == 0.0:
            gaps.append("no local altitude norm (fewer than 3 aircraft); compared with 0 ft")
        if "mlat_pairs" not in f:
            gaps.append("no radio-timing (MLAT) cross-check for this aircraft")
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
            f"Seen: {', '.join(_SIGNAL_WORDS[k] for k in fired)}. Would not fire with fewer than "
            f"{c['min_signals']} independent signals and no radio-timing (MLAT) disagreement"
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
                bands=[Band.L1_GNSS],
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
