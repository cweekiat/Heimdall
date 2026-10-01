"""Military air activity and build-up indicators per theatre (task 039). Pure: no I/O.

Inputs: `MilTracks` (decimated military tracks, 72 h, from readsb `/v2/mil` worldwide), an identity
per hex (`milroles.MilIdent`, plane-alert-db + type code), a theatre (config preset or analyst
area), a presence baseline and context counts. Indicators come from public reporting on the June
2025 Midnight Hammer strike (docs/tasks/039-military.md): tanker deployment waves to staging bases,
airlift surges, ISR presence, bomber / airborne command post activity, notices and GNSS jamming.

* MIL_DEPLOYMENT: >= min_arrivals[role] aircraft of a strategic role seen outside the theatre
  and later inside it, within `deploy_window_h`.
* MIL_SURGE: distinct aircraft of a role airborne in the theatre (6 h) >= surge_min, >= ratio x
  and z >= surge_z above its own hourly baseline.
* MIL_HIGH_INTEREST: bomber or airborne command post (E-4B / E-6B) airborne in the theatre (1 h).
  Heads of state and VIP jets are shown in the Military panel only (daily traffic, no indicator).
* MIL_BUILDUP: >= buildup_min_groups independent indicator groups within `buildup_window_h`, at
  least one a movement (tanker, airlift, bomber).

Limits (on every Evidence): only aircraft broadcasting ADS-B and flagged military are seen;
strike packages often fly with transponders off or Mode-S only, refuel beyond receiver range and
use decoys. Absence of indicators is never evidence of absence. These rules never predict a
target or a strike.
All thresholds are priors (not backtested): see config `detectors.military`.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from heimdall.detect.common import MAD_SCALE, evidence_id
from heimdall.geo import EARTH_RADIUS_KM, h3_cell
from heimdall.milroles import HIGH_INTEREST_ROLES, MilIdent
from heimdall.miltrack import MilTracks
from heimdall.schemas import AlertKind, Band, Coverage, Evidence, RecordRef, RuleResult

NAME = "military"
VERSION = "military-1.0.0"
METRIC = "mil_presence_6h"  # distinct aircraft of a role airborne in a theatre over 6 h

DEFAULTS: dict[str, Any] = {
    "deploy_window_h": 48,
    "outside_margin_km": 150,  # "came from outside" = seen this far beyond the theatre edge
    "min_arrivals": {"tanker": 6, "airlift": 6, "bomber": 2, "isr": 3, "c2": 1},
    "presence_h": 6,
    "surge_min": {"tanker": 4, "airlift": 4, "isr": 3, "bomber": 2, "fighter": 10},
    "surge_ratio": 2.0,
    "surge_z": 3.0,
    "surge_min_samples": 72,  # hourly samples (3 days) before a surge can be judged
    "high_interest_h": 1,
    "buildup_window_h": 72,
    "buildup_min_groups": 3,
    "isr_min": 2,  # distinct ISR aircraft airborne in the theatre over the build-up window
    "confidence": 0.6,
    "step_min": 5,
}
LIMITS = (
    "only aircraft broadcasting ADS-B and flagged military are seen; strike packages often fly "
    "dark (transponder off or Mode-S only), refuel beyond receiver range and use decoys"
)
ROLE_WORDS = {
    "tanker": "tankers",
    "airlift": "transport aircraft",
    "bomber": "bombers",
    "isr": "surveillance aircraft",
    "c2": "airborne command posts",
    "fighter": "fighters",
}


ALL_RULES = ("deployment", "surge", "high_interest", "buildup")


@dataclass(frozen=True)
class Theatre:
    name: str
    lat: float
    lon: float
    radius_km: float
    rules: tuple[str, ...] = ALL_RULES  # e.g. only high_interest for a home theatre (CONUS)


@dataclass
class Snapshot:
    """All track points as arrays (built once per tick, shared by theatres)."""

    hexes: list[str]
    idx: np.ndarray  # point -> index into hexes
    t: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    alt: np.ndarray

    @classmethod
    def of(cls, tracks: MilTracks) -> Snapshot:
        hexes = sorted(tracks.tracks)
        rows = [(i, *p) for i, h in enumerate(hexes) for p in tracks.tracks[h].points]
        a = np.array(rows, dtype=float) if rows else np.zeros((0, 6))
        return cls(hexes, a[:, 0].astype(int), a[:, 1], a[:, 2], a[:, 3], a[:, 4])

    def dist_km(self, lat: float, lon: float) -> np.ndarray:
        p1, p2 = np.radians(self.lat), np.radians(lat)
        dl = np.radians(self.lon - lon)
        h = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
        return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(h, 0, 1)))


def _cfg(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    c = {**DEFAULTS, **{k: v for k, v in (cfg or {}).items() if v is not None}}
    for k in ("min_arrivals", "surge_min"):
        c[k] = {**DEFAULTS[k], **(cfg or {}).get(k, {})}
    return c


def presence(
    snap: Snapshot, th: Theatre, idents: Mapping[str, MilIdent], since: float
) -> dict[str, set[str]]:
    """Role -> distinct hexes airborne inside the theatre since `since` (epoch s)."""
    sel = (snap.t >= since) & (snap.alt >= 0) & (snap.dist_km(th.lat, th.lon) <= th.radius_km)
    out: dict[str, set[str]] = defaultdict(set)
    for i in set(snap.idx[sel].tolist()):
        h = snap.hexes[i]
        ident = idents.get(h)
        if ident is not None:
            out[ident.role].add(h)
    return out


def arrivals(
    snap: Snapshot, th: Theatre, idents: Mapping[str, MilIdent], since: float, margin_km: float
) -> dict[str, list[tuple[str, float, float]]]:
    """Role -> [(hex, first outside t, first inside-after-outside t)] for aircraft that came in."""
    d = snap.dist_km(th.lat, th.lon)
    inside, outside = d <= th.radius_km, d > th.radius_km + margin_km
    out: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    recent = snap.t >= since
    for i in np.unique(snap.idx[recent]).tolist():
        m = recent & (snap.idx == i)
        ts, ins, outs = snap.t[m], inside[m], outside[m]
        order = np.argsort(ts)
        ts, ins, outs = ts[order], ins[order], outs[order]
        if not outs.any():
            continue
        t_out = float(ts[np.argmax(outs)])
        later = ins & (ts > t_out)
        if not later.any():
            continue
        h = snap.hexes[i]
        ident = idents.get(h)
        if ident is not None:
            out[ident.role].append((h, t_out, float(ts[np.argmax(later)])))
    return out


def _evidence(
    kind: AlertKind,
    th: Theatre,
    entity: str,
    start: datetime,
    end: datetime,
    now: datetime,
    *,
    score: float,
    conf: float,
    rules: list[RuleResult],
    features: dict[str, float],
    hexes: Sequence[str],
    counterfactual: str,
    gaps: Sequence[str] = (),
    providers: Sequence[str] = ("adsb_mil",),
) -> Evidence:
    cell = h3_cell(th.lat, th.lon, 3)
    return Evidence(
        id=evidence_id(kind.value, cell, entity, start, th.name),
        kind=kind,
        detector=NAME,
        detector_version=VERSION,
        window_start=start,
        window_end=end,
        aoi=th.name,
        h3_cell=cell,
        h3_res=3,
        lat=th.lat,
        lon=th.lon,
        score=round(max(0.0, min(1.0, score)), 4),
        rules=rules,
        features=features,
        entities=list(hexes)[:50],
        records=[RecordRef(source="adsb_mil", id=h) for h in list(hexes)[:50]],
        counterfactual=counterfactual,
        coverage=Coverage(
            n_observed=len(hexes),
            n_quality=len(hexes),
            providers=list(providers),
            confidence=round(conf, 3),
        ),
        gaps=[*gaps, LIMITS],
        created_at=now,
        bands=[Band.NONE],
    )


def _floor(now: datetime, minutes: int) -> datetime:
    s = int(now.timestamp()) // (minutes * 60) * (minutes * 60)
    return datetime.fromtimestamp(s, tz=now.tzinfo)


def detect_movements(
    snap: Snapshot,
    th: Theatre,
    idents: Mapping[str, MilIdent],
    now: datetime,
    cfg: Mapping[str, Any] | None = None,
    baseline: Callable[[str], list[float]] | None = None,
) -> tuple[list[Evidence], dict[str, float]]:
    """MIL_DEPLOYMENT, MIL_SURGE, MIL_HIGH_INTEREST for one theatre, plus the presence counts the
    pipeline records as tomorrow's baseline ({role: distinct aircraft airborne, last 6 h})."""
    c = _cfg(cfg)
    t_now = now.timestamp()
    out: list[Evidence] = []
    end = _floor(now, int(c["step_min"]))  # aligned to the tick grid (stable ids per tick)

    # deployment waves
    dep_from = end - timedelta(hours=float(c["deploy_window_h"]))
    arr = arrivals(snap, th, idents, dep_from.timestamp(), float(c["outside_margin_km"]))
    for role, need in sorted(c["min_arrivals"].items()) if "deployment" in th.rules else []:
        got = arr.get(role, [])
        if len(got) < int(need):
            continue
        hexes = sorted(h for h, _, _ in got)
        known = sum(1 for h in hexes if idents[h].known)
        out.append(
            _evidence(
                AlertKind.MIL_DEPLOYMENT,
                th,
                role,
                # the episode starts at the first arrival, not at the look-back edge
                max(
                    dep_from,
                    _floor(
                        datetime.fromtimestamp(min(x[2] for x in got), tz=now.tzinfo),
                        int(c["step_min"]),
                    ),
                ),
                end,
                now,
                score=len(got) / (3.0 * int(need)),
                conf=float(c["confidence"]) * (0.8 + 0.2 * known / len(hexes)),
                rules=[
                    RuleResult(
                        rule=f"D1_{role}_arrivals",
                        description=f"{len(got)} {ROLE_WORDS.get(role, role)} flew into {th.name} "
                        f"from outside in {c['deploy_window_h']} h",
                        value=float(len(got)),
                        threshold=float(need),
                        op=">=",
                        passed=True,
                    )
                ],
                features={"n": float(len(got)), "known": float(known), f"role_{role}": 1.0},
                hexes=hexes,
                counterfactual=f"Would not fire with fewer than {need} arrivals.",
            )
        )

    # surge vs own baseline
    pres = presence(snap, th, idents, t_now - float(c["presence_h"]) * 3600)
    counts = {r: float(len(v)) for r, v in pres.items()}
    for role, need in sorted(c["surge_min"].items()) if "surge" in th.rules else []:
        n = counts.get(role, 0.0)
        hist = baseline(role) if baseline is not None else []
        if n < float(need) or len(hist) < int(c["surge_min_samples"]):
            continue
        med = statistics.median(hist)
        mad = statistics.median(abs(v - med) for v in hist)
        z = (n - med) / (MAD_SCALE * mad + 1.0)
        ratio = n / (med + 1.0)
        if z < float(c["surge_z"]) or ratio < float(c["surge_ratio"]):
            continue
        hexes = sorted(pres[role])
        out.append(
            _evidence(
                AlertKind.MIL_SURGE,
                th,
                role,
                end - timedelta(hours=float(c["presence_h"])),
                end,
                now,
                score=min(z / (2 * float(c["surge_z"])), ratio / (2 * float(c["surge_ratio"]))),
                conf=float(c["confidence"]),
                rules=[
                    RuleResult(
                        rule=f"S1_{role}_presence",
                        description=f"{n:.0f} {ROLE_WORDS.get(role, role)} airborne in {th.name} "
                        f"(last {c['presence_h']} h) vs usual {med:.1f}",
                        value=n,
                        threshold=float(need),
                        op=">=",
                        passed=True,
                        baseline=med,
                    )
                ],
                features={
                    "n": n,
                    "baseline": med,
                    "z": round(z, 2),
                    "ratio": round(ratio, 2),
                    f"role_{role}": 1.0,
                },
                hexes=hexes,
                counterfactual=f"Would not fire below {need} aircraft, ratio "
                f"{c['surge_ratio']:g} or z {c['surge_z']:g}.",
            )
        )

    # high-interest aircraft
    hi = presence(snap, th, idents, t_now - float(c["high_interest_h"]) * 3600)
    # alerts only for bombers and airborne command posts; heads of state / VIP jets stay in the
    # Military panel (they fly every day and are not build-up indicators)
    hexes = sorted(h for v in hi.values() for h in v if idents[h].role in HIGH_INTEREST_ROLES)
    if "high_interest" not in th.rules:
        hexes = []
    if hexes:
        kinds = sorted({idents[h].type_name or ROLE_WORDS.get(idents[h].role, "") for h in hexes})
        strategic = True
        out.append(
            _evidence(
                AlertKind.MIL_HIGH_INTEREST,
                th,
                "high_interest",
                end - timedelta(hours=float(c["high_interest_h"])),
                end,
                now,
                score=0.7 if strategic else 0.4,
                conf=float(c["confidence"]),
                rules=[
                    RuleResult(
                        rule="H1_high_interest_airborne",
                        description=f"{len(hexes)} high-interest aircraft airborne in {th.name}: "
                        + ", ".join(kinds),
                        value=float(len(hexes)),
                        threshold=1.0,
                        op=">=",
                        passed=True,
                    )
                ],
                features={"n": float(len(hexes)), "strategic": 1.0 if strategic else 0.0},
                hexes=hexes,
                counterfactual="Would not fire if none of these aircraft were airborne here.",
            )
        )
    return out, counts


GROUPS = ("tanker", "airlift", "bomber_c2", "isr", "notice", "gnss_jamming")
MOVEMENT = ("tanker", "airlift", "bomber_c2")


def detect_buildup(
    movement_evidence: Sequence[Evidence],
    snap: Snapshot,
    th: Theatre,
    idents: Mapping[str, MilIdent],
    now: datetime,
    cfg: Mapping[str, Any] | None = None,
    *,
    n_notices: int = 0,
    n_jamming: int = 0,
) -> list[Evidence]:
    """MIL_BUILDUP: several independent indicator groups in one theatre within the window.

    `movement_evidence`: this theatre's MIL_* Evidence from the last `buildup_window_h` (stored +
    this tick); `n_notices`: active military / airspace notices in the theatre; `n_jamming`:
    GNSS-jamming Evidence in the theatre over the window."""
    c = _cfg(cfg)
    if "buildup" not in th.rules:
        return []
    end = _floor(now, int(c["step_min"]))
    start = end - timedelta(hours=float(c["buildup_window_h"]))
    groups: dict[str, str] = {}
    hexes: set[str] = set()
    for e in movement_evidence:
        if e.aoi != th.name or e.window_end < start:
            continue
        roles = [k[5:] for k, v in e.features.items() if k.startswith("role_") and v]
        for r in roles:
            g = "bomber_c2" if r in ("bomber", "c2") else r
            if g in GROUPS and g not in groups:
                groups[g] = e.rules[0].description
                hexes |= set(e.entities)
        if e.kind == AlertKind.MIL_HIGH_INTEREST and e.features.get("strategic"):
            groups.setdefault("bomber_c2", e.rules[0].description)
            hexes |= set(e.entities)
    isr = presence(snap, th, idents, start.timestamp()).get("isr", set())
    if len(isr) >= int(c["isr_min"]):
        groups.setdefault("isr", f"{len(isr)} surveillance aircraft airborne in {th.name}")
        hexes |= isr
    if n_notices:
        groups["notice"] = f"{n_notices} active military / airspace notices in {th.name}"
    if n_jamming:
        groups["gnss_jamming"] = f"GNSS jamming evidence in {th.name} ({n_jamming} items)"
    starts = [
        e.window_start for e in movement_evidence if e.aoi == th.name and e.window_end >= start
    ]
    if starts:
        start = max(start, min(starts))
    moving = [g for g in MOVEMENT if g in groups]
    need = int(c["buildup_min_groups"])
    if len(groups) < need or not moving:
        return []
    rules = [
        RuleResult(
            rule=f"B_{g}",
            description=groups.get(g, f"no {g.replace('_', ' ')} indicator"),
            value=1.0 if g in groups else 0.0,
            threshold=1.0,
            op=">=",
            passed=g in groups,
        )
        for g in GROUPS
    ]
    return [
        _evidence(
            AlertKind.MIL_BUILDUP,
            th,
            "buildup",
            start,
            end,
            now,
            score=len(groups) / len(GROUPS) + 0.15,
            conf=float(c["confidence"]),
            rules=rules,
            features={"groups": float(len(groups)), **{f"g_{g}": 1.0 for g in groups}},
            hexes=sorted(hexes),
            counterfactual=f"Would not fire with fewer than {need} indicator groups or without "
            "a tanker, airlift or bomber movement.",
            gaps=["indicator checklist only: it never predicts a target or a strike"],
        )
    ]
