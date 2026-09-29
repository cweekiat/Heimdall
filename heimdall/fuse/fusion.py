"""Fusion v0: Evidence -> Alert. Pure; the pipeline supplies open alerts and persists results.

Steps (see docs/detectors.md "Fusion v0"):
1. Persistence gate: evidence.persistence >= persistence_windows[kind].
2. Group gated evidence by (kind, h3_cell); merge into the open (NEW/CONFIRMED) alert for the same
   kind+cell (extend evidence_ids, last_seen; severity never downgrades while open) or open a
   new alert with a deterministic id sha1(alert|kind|cell|first_seen).
3. CRITICAL composite: >= critical_min_tankers distinct tanker orbits within jam_proximity_km of
   a gated jamming Evidence.
4. Severity from s = lead.score x lead.coverage.confidence; control AOIs capped at INFO.
5. Admiralty grade, corroborations (co-located other-kind Evidence = context), template summary
   (no LLM), recommended_actions from playbook `applies_to`.
6. Open alerts not refreshed for expire_after_min -> EXPIRED.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from heimdall.fuse.admiralty import KIND_DOMAIN, grade
from heimdall.geo import haversine_km
from heimdall.schemas import (
    Alert,
    AlertKind,
    AlertStatus,
    Corroboration,
    Domain,
    Evidence,
    Severity,
)

DEFAULTS: dict[str, Any] = {
    "persistence_windows": {"gnss_jamming": 2, "gnss_spoofing": 2, "mil_orbit": 2},
    "default_persistence": 2,
    "severity": {"high": 0.6, "medium": 0.35, "low": 0.15},
    "strong_score": 0.6,
    "expire_after_min": 60,
    "max_evidence_ids": 100,
    "jam_proximity_km": 100,
    "critical_min_tankers": 2,
}

_SEV_ORDER = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
OPEN = (AlertStatus.NEW, AlertStatus.CONFIRMED)
CorrStatus = Literal["corroborated", "uncorroborated", "contradicted"]


def _cfg(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg or {}).items() if v is not None})
    return out


def required_persistence(kind: AlertKind | str, cfg: Mapping[str, Any] | None = None) -> int:
    c = _cfg(cfg)
    return int(c["persistence_windows"].get(str(kind), c["default_persistence"]))


def severity_for(score: float, confidence: float, cfg: Mapping[str, Any] | None = None) -> Severity:
    th = _cfg(cfg)["severity"]
    s = score * confidence
    if s >= th["high"]:
        return Severity.HIGH
    if s >= th["medium"]:
        return Severity.MEDIUM
    if s >= th["low"]:
        return Severity.LOW
    return Severity.INFO


def _max_sev(a: Severity, b: Severity) -> Severity:
    return a if _SEV_ORDER.index(a) >= _SEV_ORDER.index(b) else b


def alert_id(kind: str, cell: str | None, first_seen: datetime) -> str:
    return hashlib.sha1(f"alert|{kind}|{cell}|{first_seen.isoformat()}".encode()).hexdigest()


def actions_for(kind: AlertKind | str, playbook: Mapping[str, Any] | None) -> list[str]:
    acts = (playbook or {}).get("actions", {}) or {}
    out = []
    for aid, spec in acts.items():
        applies = (spec or {}).get("applies_to", []) or []
        if "*" in applies or str(kind) in applies:
            out.append(str(aid))
    return out


def _f(e: Evidence, k: str, default: float = 0.0) -> float:
    return e.features.get(k, default)


def _summary(kind: AlertKind, group: Sequence[Evidence], lead: Evidence, cell: str | None) -> str:
    prov = ", ".join(lead.coverage.providers) or "no providers"
    cov = f"coverage {lead.coverage.confidence:.0%} ({prov})"
    pers = f"held {lead.persistence} consecutive windows"
    ents = sorted({x for e in group for x in e.entities})
    if kind == AlertKind.GNSS_JAMMING:
        rr = next((r for r in lead.rules if r.rule == "degraded_ratio"), None)
        base = "n/a" if rr is None or rr.baseline is None else f"{rr.baseline:.2f}"
        thr = "n/a" if rr is None else rr.threshold
        return (
            f"GNSS degradation in H3 {cell}: {_f(lead, 'bad'):.0f}/{_f(lead, 'total'):.0f} "
            f"true-ADS-B aircraft degraded (R'={_f(lead, 'ratio'):.2f} vs threshold {thr}, "
            f"baseline median {base}); {pers}; {cov}. Estimated affected area "
            f"{_f(lead, 'ellipse_major_km'):.0f} x {_f(lead, 'ellipse_minor_km'):.0f} km (2-sigma)."
        )
    if kind == AlertKind.GNSS_SPOOFING:
        sigs = sorted({s for e in group for s in ("S1", "S2", "S3", "S4", "S5") if _f(e, s)})
        return (
            f"Possible GNSS spoofing in H3 {cell}: {len(ents)} aircraft "
            f"({', '.join(ents[:5])}{'...' if len(ents) > 5 else ''}) with signals "
            f"{', '.join(sigs)}; {pers}; {cov}."
        )
    if kind == AlertKind.MIL_ORBIT:
        return (
            f"Military orbit in H3 {cell}: {len(ents)} aircraft ({', '.join(ents[:5])}); "
            f"{_f(lead, 'laps'):.1f} laps, radius {_f(lead, 'radius_km'):.0f} km, "
            f"novelty {_f(lead, 'novelty'):.2f}; {pers}; {cov}."
        )
    if kind == AlertKind.COMPOSITE:
        jam = next((e for e in group if e.kind == AlertKind.GNSS_JAMMING), lead)
        tk = sorted({x for e in group if e.kind == AlertKind.MIL_ORBIT for x in e.entities})
        return (
            f"CRITICAL composite in H3 {cell}: {len(tk)} tanker orbits ({', '.join(tk)}) near "
            f"active GNSS jamming (R'={_f(jam, 'ratio'):.2f}, {jam.persistence} windows)."
        )
    return f"{kind} in H3 {cell}: {len(group)} evidence items; {pers}; {cov}."


_TITLES = {
    AlertKind.GNSS_JAMMING: "GNSS jamming",
    AlertKind.GNSS_SPOOFING: "GNSS spoofing",
    AlertKind.MIL_ORBIT: "Military orbit",
    AlertKind.COMPOSITE: "Tanker orbits near GNSS jamming",
}


def _composites(
    gated: Sequence[Evidence], c: Mapping[str, Any]
) -> list[tuple[Evidence, list[Evidence]]]:
    jams = [e for e in gated if e.kind == AlertKind.GNSS_JAMMING and e.lat is not None]
    tankers = [
        e
        for e in gated
        if e.kind == AlertKind.MIL_ORBIT and _f(e, "is_tanker") >= 1.0 and e.lat is not None
    ]
    out = []
    for j in jams:
        near = [
            t
            for t in tankers
            if haversine_km(j.lat, j.lon, t.lat, t.lon)  # type: ignore[arg-type]
            <= c["jam_proximity_km"]
        ]
        if len({x for t in near for x in t.entities}) >= c["critical_min_tankers"]:
            out.append((j, near))
    return out


def fuse(
    evidence: Sequence[Evidence],
    open_alerts: Sequence[Alert],
    now: datetime,
    cfg: Mapping[str, Any] | None = None,
    playbook: Mapping[str, Any] | None = None,
    source_grades: Mapping[str, str] | None = None,
    *,
    aoi: str | None = None,
    control: bool = False,
) -> list[Alert]:
    """Return new/updated/expired alerts (caller upserts them)."""
    c = _cfg(cfg)
    grades = source_grades or {}
    gated = [e for e in evidence if e.persistence >= required_persistence(e.kind, c)]
    groups: dict[tuple[AlertKind, str | None], list[Evidence]] = defaultdict(list)
    for e in gated:
        groups[(e.kind, e.h3_cell)].append(e)
    for j, orbits in _composites(gated, c):
        groups[(AlertKind.COMPOSITE, j.h3_cell)] = [j, *orbits]

    by_key = {(a.kind, a.h3_cell): a for a in open_alerts if a.status in OPEN}
    by_cell: dict[str | None, list[Evidence]] = defaultdict(list)
    for e in gated:
        by_cell[e.h3_cell].append(e)

    out: list[Alert] = []
    touched: set[str] = set()
    for (kind, cell), group in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        lead = max(group, key=lambda e: (e.score * e.coverage.confidence, e.id))
        if kind == AlertKind.COMPOSITE:
            lead = group[0]
            sev = Severity.CRITICAL
            conf = min(e.coverage.confidence for e in group)
        else:
            sev = severity_for(lead.score, lead.coverage.confidence, c)
            conf = lead.coverage.confidence
        if control:
            sev = Severity.INFO
        dom = KIND_DOMAIN.get(kind, Domain.AIR)
        corr = [
            Corroboration(
                domain=KIND_DOMAIN.get(o.kind, Domain.AIR),
                evidence_id=o.id,
                relation="context",
                note=f"co-located {o.kind} evidence in the same cell",
            )
            for o in by_cell.get(cell, [])
            if o.kind != kind and o not in group
        ]
        supports_other = any(x.relation == "supports" and x.domain != dom for x in corr)
        status_c: CorrStatus = (
            "contradicted"
            if any(x.relation == "contradicts" for x in corr)
            else ("corroborated" if supports_other else "uncorroborated")
        )
        prev = by_key.get((kind, cell))
        adm = grade(
            group,
            corr,
            grades,
            required_persistence=required_persistence(lead.kind, c),
            strong_score=c["strong_score"],
            analyst_confirmed=prev is not None and prev.status == AlertStatus.CONFIRMED,
        )
        ev_ids = [e.id for e in sorted(group, key=lambda e: e.id)]
        first = min(e.window_start for e in group)
        last = max(e.window_end for e in group)
        summary = _summary(kind, group, lead, cell) + f" Admiralty {adm}."
        if control:
            summary += " [control AOI: calibration only, severity capped at info]"
        title = f"{_TITLES.get(kind, str(kind))}" + (f" — {aoi}" if aoi else "")
        if prev is not None:
            ids = list(dict.fromkeys([*prev.evidence_ids, *ev_ids]))[-c["max_evidence_ids"] :]
            alert = prev.model_copy(
                update={
                    "severity": sev if control else _max_sev(prev.severity, sev),
                    "confidence": round(conf, 4),
                    "admiralty": adm,
                    "summary": summary,
                    "evidence_ids": ids,
                    "corroborations": corr,
                    "corroboration_status": status_c,
                    "last_seen": max(prev.last_seen, last),
                    "lat": lead.lat,
                    "lon": lead.lon,
                }
            )
        else:
            alert = Alert(
                id=alert_id(str(kind), cell, first),
                kind=kind,
                severity=sev,
                confidence=round(min(1.0, max(0.0, conf)), 4),
                admiralty=adm,
                title=title,
                summary=summary,
                aoi=aoi,
                h3_cell=cell,
                lat=lead.lat,
                lon=lead.lon,
                evidence_ids=ev_ids,
                corroborations=corr,
                corroboration_status=status_c,
                recommended_actions=actions_for(kind, playbook),
                status=AlertStatus.NEW,
                first_seen=first,
                last_seen=last,
            )
        touched.add(alert.id)
        out.append(alert)

    horizon = now - timedelta(minutes=c["expire_after_min"])
    for a in open_alerts:
        if a.status in OPEN and a.id not in touched and a.last_seen < horizon:
            out.append(a.model_copy(update={"status": AlertStatus.EXPIRED}))
    return out
