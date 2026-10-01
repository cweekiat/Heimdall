"""GNSS L1 interference seen from orbit: CYGNSS noise floor per H3 r3 cell-day (task 038, ADR-008).

Input: `l1_noise` GeoEvents from `heimdall/ingest/cygnss.py`, one per cell and UTC day. `value`
= median DDM noise floor in dB above the satellite channel's open-ocean median for that day; attrs
carry n, n_sats, ocean_frac, rfi_frac and the cell's own trailing baseline (base_db, base_mad_db,
base_days). Pure: no I/O.

Rules (all priors from the 2026-09-12..29 backfill, `docs/reports/2026-10-01-cygnss.md`):
* C1 level (open ocean only, ocean_frac >= 0.9): excess >= level_db. Over open ocean the global
  99th percentile is ~1.5 dB; warm land in the beam adds at most ~2 dB near large deserts.
* C1 land/coast level (ocean_frac < 0.9): excess >= land_level_db (3.5 dB = 99.5th pct of
  non-ocean cell-days 2026-09-20..27; >= 3.5 dB clustered almost only over S Iran / the Gulf). A
  persistent jammer is its own baseline, so only an absolute level can see it.
* C2 change: excess - own baseline >= change_db and robust z >= z_min, with >= min_baseline_days
  of history (needed on land and coasts, where the absolute level means little).
* C3 (supporting only): share of DDMs with the `rfi_detected` kurtosis flag vs its baseline.
A cell-day is flagged when (C1 or C2) and n >= min_samples. Persistence = flagged days among the
cell's last `persist_days` observed days (the latest must be flagged); fusion gates on >= 2.

Coverage and limits stated on every Evidence: +-38 deg latitude only, 2-3 day latency, location
only as good as the antenna footprint (tens to hundreds of km), absence of a flag is not absence
of jamming. Explained away: a strong solar radio event (SWPC R >= r_scale_explain) on that day
lowers confidence (solar radio bursts raise L-band noise on the day side). Confidence is capped
so a CYGNSS-only alert stays <= medium (ADR-008 s4); it never names an emitter type.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from heimdall.detect.common import MAD_SCALE, cfg_get, evidence_id
from heimdall.geo import KM_PER_NM, haversine_km
from heimdall.schemas import (
    AlertKind,
    Band,
    Coverage,
    Evidence,
    GeoEvent,
    RecordRef,
    RuleResult,
)

NAME = "cygnss"
VERSION = "cygnss-1.0.0"
KIND = "l1_noise"

DEFAULTS: dict[str, Any] = {
    "max_age_days": 5,  # newest data day considered (CYGNSS L1 latency is 2-3 days)
    "min_samples": 30,
    "level_db": 2.5,
    "ocean_frac_min": 0.9,
    "land_level_db": 3.5,
    "change_db": 1.5,
    "z_min": 4.0,
    "mad_floor_db": 0.2,
    "min_baseline_days": 7,
    "persist_days": 3,
    "reach_km": 50,
    "confidence_cap": 0.55,  # single experimental band: score x conf < 0.6 -> <= medium
    "full_samples": 200,
    "explained_factor": 0.5,
    "r_scale_explain": 3,
}


class AoiLike(Protocol):
    name: str
    lat: float
    lon: float
    radius_nm: float


@dataclass
class CellDay:
    event: GeoEvent
    excess: float
    delta: float | None
    z: float | None
    c1: bool
    c2: bool
    flagged: bool


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def assess(e: GeoEvent, c: dict[str, Any]) -> CellDay:
    a = e.attrs
    n = _num(a.get("n")) or 0.0
    ex = float(e.value or 0.0)
    ocean = _num(a.get("ocean_frac")) or 0.0
    base, mad = _num(a.get("base_db")), _num(a.get("base_mad_db"))
    bdays = _num(a.get("base_days")) or 0.0
    delta = z = None
    if base is not None and mad is not None and bdays >= c["min_baseline_days"]:
        delta = ex - base
        z = delta / (MAD_SCALE * mad + float(c["mad_floor_db"]))
    level = c["level_db"] if ocean >= c["ocean_frac_min"] else c["land_level_db"]
    c1 = ex >= level
    c2 = delta is not None and z is not None and delta >= c["change_db"] and z >= c["z_min"]
    return CellDay(e, ex, delta, z, c1, c2, (c1 or c2) and n >= c["min_samples"])


def _space_weather_note(sw: Sequence[GeoEvent], day: date, r_min: float) -> str | None:
    rs = [
        _num(e.attrs.get("r_scale")) or 0.0
        for e in sw
        if e.kind == "space_weather" and e.ts.date() == day
    ]
    r = max(rs, default=0.0)
    if r >= r_min:
        return f"strong solar radio event (R{int(r)}) that day can raise L-band noise"
    return None


def detect(
    events: Sequence[GeoEvent],
    cfg: dict[str, Any],
    now: datetime,
    *,
    aoi: AoiLike,
    space_weather: Sequence[GeoEvent] = (),
) -> list[Evidence]:
    """One GNSS_JAMMING Evidence per r3 cell flagged on its latest observed day."""
    c = {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}
    lim = aoi.radius_nm * KM_PER_NM + float(c["reach_km"])
    oldest = (now - timedelta(days=float(c["max_age_days"]))).date()
    by_cell: dict[str, dict[date, CellDay]] = defaultdict(dict)
    for e in events:
        if e.kind != KIND or e.lat is None or e.lon is None or e.ts.date() < oldest:
            continue
        if e.ts > now or haversine_km(e.lat, e.lon, aoi.lat, aoi.lon) > lim:
            continue
        cell = str(e.attrs.get("h3") or "")
        if cell:
            by_cell[cell][e.ts.date()] = assess(e, c)

    out: list[Evidence] = []
    for cell, days in sorted(by_cell.items()):
        recent = [days[d] for d in sorted(days)][-int(c["persist_days"]) :]
        last = recent[-1]
        if not last.flagged:
            continue
        persistence = sum(1 for x in recent if x.flagged)
        e, a = last.event, last.event.attrs
        day = e.ts.date()
        n = int(_num(a.get("n")) or 0)
        n_sats = int(_num(a.get("n_sats")) or 0)
        ocean = _num(a.get("ocean_frac")) or 0.0
        rfi, rfi0 = _num(a.get("rfi_frac")), _num(a.get("base_rfi_frac"))
        base = _num(a.get("base_db"))
        open_sea = ocean >= c["ocean_frac_min"]
        level = float(c["level_db"] if open_sea else c["land_level_db"])
        s1 = last.excess / (2 * level) if last.c1 else 0.0
        s2 = (last.delta or 0.0) / (2 * float(c["change_db"])) if last.c2 else 0.0
        score = max(0.0, min(1.0, max(s1, s2)))
        conf = min(1.0, n / float(c["full_samples"])) * (1.0 if n_sats >= 2 else 0.8)
        conf = min(float(c["confidence_cap"]), conf)
        gaps = [
            "seen from orbit by CYGNSS (+-38 deg latitude, 2-3 day latency); location only as "
            "good as the antenna footprint (tens to hundreds of km)",
            "warm land in the antenna beam also raises the noise floor (up to ~2 dB)",
            "experimental: it never names an emitter type",
        ]
        note = _space_weather_note(space_weather, day, float(c["r_scale_explain"]))
        if note:
            conf *= float(c["explained_factor"])
            gaps.insert(0, note)
        if base is None or (_num(a.get("base_days")) or 0) < c["min_baseline_days"]:
            gaps.append(f"short baseline: {int(_num(a.get('base_days')) or 0)} days")
        start = datetime(day.year, day.month, day.day, tzinfo=e.ts.tzinfo)
        rules = [
            RuleResult(
                rule="C1_noise_level",
                description=f"L1 noise floor {last.excess:+.1f} dB vs the satellites' open-ocean "
                f"median ({ocean:.0%} of reflections over open water; "
                f"{'open-ocean' if open_sea else 'land/coast'} threshold)",
                value=round(last.excess, 3),
                threshold=level,
                op=">=",
                passed=last.c1,
                baseline=0.0,
            ),
            RuleResult(
                rule="C2_change_vs_own_baseline",
                description="Rise of the noise floor above this cell's own typical level"
                + ("" if last.delta is None else f" ({last.delta:+.1f} dB, z {last.z:.1f})"),
                value=round(last.delta, 3) if last.delta is not None else 0.0,
                threshold=float(c["change_db"]),
                op=">=",
                passed=last.c2,
                baseline=base,
            ),
        ]
        if rfi is not None:
            rules.append(
                RuleResult(
                    rule="C3_rfi_flag_share",
                    description="Share of reflections flagged rfi_detected by CYGNSS "
                    "(supporting only)",
                    value=round(rfi, 4),
                    threshold=round(max(0.1, 2 * (rfi0 or 0.0)), 4),
                    op=">=",
                    passed=rfi >= max(0.1, 2 * (rfi0 or 0.0)),
                    baseline=rfi0,
                )
            )
        lim_txt = []
        if last.c1:
            lim_txt.append(f"the excess were below {level:g} dB")
        if last.c2:
            lim_txt.append(f"the rise over its own baseline were below {c['change_db']:g} dB")
        out.append(
            Evidence(
                id=evidence_id(AlertKind.GNSS_JAMMING.value, cell, NAME, start, aoi.name),
                kind=AlertKind.GNSS_JAMMING,
                detector=NAME,
                detector_version=VERSION,
                window_start=start,
                window_end=start + timedelta(days=1),
                aoi=aoi.name,
                h3_cell=cell,
                h3_res=3,
                lat=e.lat,
                lon=e.lon,
                score=round(score, 4),
                rules=rules,
                features={
                    "excess_db": round(last.excess, 3),
                    "baseline_db": base if base is not None else 0.0,
                    "delta_db": round(last.delta, 3) if last.delta is not None else 0.0,
                    "z": round(last.z, 2) if last.z is not None else 0.0,
                    "n": float(n),
                    "n_sats": float(n_sats),
                    "ocean_frac": ocean,
                    "rfi_frac": rfi or 0.0,
                    "persist_days": float(persistence),
                },
                records=[RecordRef(source=x.event.source, id=x.event.id) for x in recent],
                counterfactual="Would not fire if " + " and ".join(lim_txt) + ".",
                coverage=Coverage(
                    n_observed=n,
                    n_quality=n,
                    providers=sorted({x.event.source for x in recent}),
                    confidence=round(conf, 3),
                ),
                gaps=gaps,
                persistence=persistence,
                created_at=now,
                bands=[Band.L1_GNSS],
            )
        )
    return out
