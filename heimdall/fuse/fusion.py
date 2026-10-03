"""Fusion v0: Evidence -> Alert. Pure; the pipeline supplies open alerts and persists results.

Steps (see docs/detectors.md "Fusion v0"):
1. Persistence gate: evidence.persistence >= persistence_windows[kind].
2. Group gated evidence by (kind, h3_cell); merge into the open (NEW/CONFIRMED) alert for the same
   kind+cell (extend evidence_ids, last_seen; severity never downgrades while open) or open a
   new alert with a deterministic id sha1(alert|kind|cell|first_seen).
3. CRITICAL composite: >= critical_min_tankers distinct tanker orbits within jam_proximity_km of
   a gated jamming Evidence.
4. Severity from s = lead.score x lead.coverage.confidence; control AOIs capped at INFO.
5. Corroborations from `fuse/corroborate.py` (ADR-005: notices, AIS/jamming; co-located
   other-kind Evidence = context). Credibility counts independent source *families*. Admiralty
   grade, template summary (no LLM, no notice text), recommended_actions from playbook `applies_to`.
   WARNING_AREA Evidence is context-only: it never opens an alert.
6. Context (ADR-007 s.4-5, task 022b): `confounders.assess` on the lead Evidence multiplies the
   alert confidence by a factor in [min_factor, 1] (weather, Kp, scintillation); the notes are
   appended as `Corroboration(relation="context", factor=...)` and the gaps go to `Alert.gaps`.
   `observability.observability` appends footprint/overpass context notes. Severity is computed
   before and never touched by this; Admiralty and the corroboration status never count context.
7. Open alerts not refreshed for expire_after_min -> EXPIRED.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from heimdall.fuse import confounders, observability
from heimdall.fuse.admiralty import grade, independent_families, source_key
from heimdall.fuse.corroborate import (
    DEFAULT_CORROBORATION,
    DEFAULT_SOURCE_FAMILY,
    cross_source,
    family_index,
    family_of,
)
from heimdall.geo import h3_cell_size_km, h3_resolution, haversine_km
from heimdall.schemas import (
    Admiralty,
    Alert,
    AlertKind,
    AlertSource,
    AlertStatus,
    Band,
    Corroboration,
    Evidence,
    Footprint,
    GeoEvent,
    Severity,
    SourceReliability,
)

DEFAULTS: dict[str, Any] = {
    "persistence_windows": {
        "gnss_jamming": 2,
        "gnss_spoofing": 2,
        "mil_orbit": 2,
        "ais_spoofing": 2,
        "ais_gap": 2,
    },
    "default_persistence": 2,
    "severity": {"high": 0.6, "medium": 0.35, "low": 0.15},
    "strong_score": 0.6,
    "expire_after_min": 60,
    "dismiss_max_episode_h": 12,
    "max_evidence_ids": 100,
    "jam_proximity_km": 100,
    "critical_min_tankers": 2,
    "source_family": DEFAULT_SOURCE_FAMILY,
    "corroboration": DEFAULT_CORROBORATION,
    "confounders": {},  # confounders.DEFAULTS apply per missing key
    "observability": {},  # observability.DEFAULTS apply per missing key
}

_SEV_ORDER = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
CLOSED = (AlertStatus.RESOLVED, AlertStatus.DISMISSED, AlertStatus.EXPIRED)
#: ADR-003: an alert in any analyst status (new, acknowledged, under_review, shelved, confirmed)
#: is still the live alert for its kind+cell; only dismissed/expired alerts are closed.
ACTIVE = tuple(s for s in AlertStatus if s not in CLOSED)
OPEN = ACTIVE  # backwards-compatible alias
#: Fields the pipeline may refresh from Evidence. `status` is analyst-owned (ADR-003) and is only
#: ever written by the pipeline for NEW -> EXPIRED on staleness.
EVIDENCE_FIELDS = (
    "severity",
    "confidence",
    "admiralty",
    "summary",
    "evidence_ids",
    "corroborations",
    "corroboration_status",
    "gaps",
    "last_seen",
    "lat",
    "lon",
    "sources",
    "footprint",
)
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


def alert_id(kind: str, cell: str | None, first_seen: datetime, aoi: str | None = None) -> str:
    """sha1(alert|aoi|kind|cell|first_seen ISO)."""
    raw = f"alert|{aoi or ''}|{kind}|{cell}|{first_seen.isoformat()}"
    return hashlib.sha1(raw.encode()).hexdigest()


def actions_for(kind: AlertKind | str, playbook: Mapping[str, Any] | None) -> list[str]:
    acts = (playbook or {}).get("actions", {}) or {}
    out = []
    for aid, spec in acts.items():
        applies = (spec or {}).get("applies_to", []) or []
        if "*" in applies or str(kind) in applies:
            out.append(str(aid))
    return out


#: kinds that describe one moving object; the footprint is a track when exactly one entity is named
_TRACK_KINDS = (AlertKind.AIS_SPOOFING, AlertKind.AIS_GAP, AlertKind.MIL_ORBIT)


def _connector_index(
    evidence: Sequence[Evidence], notices: Sequence[GeoEvent]
) -> dict[str, set[str]]:
    """{evidence id or notice id: source keys of the connectors whose records show it}."""
    idx: dict[str, set[str]] = defaultdict(set)
    for e in evidence:
        idx[e.id] |= {source_key(r.source) for r in e.records}
    for n in notices:
        idx[n.id].add(source_key(n.source))
    return idx


def _alert_sources(
    group: Sequence[Evidence],
    corr: Sequence[Corroboration],
    conn_idx: Mapping[str, set[str]],
    adm: Admiralty,
    grades: Mapping[str, str],
    source_family: Mapping[str, str],
) -> list[AlertSource]:
    """One AlertSource per connector behind the alert: those whose own records show the anomaly
    plus those behind a *supporting* corroboration. Admiralty = connector reliability (grade from
    sources.yaml, F if unknown) x the alert's overall credibility (credibility is a property of
    the whole alert, not of one connector). Sorted by (reliability, connector)."""
    conns = {source_key(r.source) for e in group for r in e.records}
    for x in corr:
        if x.relation == "supports":
            conns |= conn_idx.get(x.evidence_id, set())
    out = []
    for c in conns:
        g = grades.get(c, "F").upper()
        rel = SourceReliability(g if g in "ABCDEF" and len(g) == 1 else "F")
        out.append(
            AlertSource(
                connector=c,
                family=family_of(c, source_family),
                admiralty=Admiralty(reliability=rel, credibility=adm.credibility),
            )
        )
    return sorted(out, key=lambda s: (s.admiralty.reliability.value, s.connector))


def _footprint(kind: AlertKind, group: Sequence[Evidence], cell: str | None) -> Footprint:
    """single_track for a one-object kind with exactly one entity, else the alert's H3 cell."""
    ents = sorted({x for e in group for x in e.entities})
    if kind in _TRACK_KINDS and len(ents) == 1:
        return Footprint(kind="single_track", description=f"Track {ents[0]}")
    if cell:
        try:
            size = h3_cell_size_km(cell)
        except Exception:  # not an H3 index (synthetic ids): describe without a size
            return Footprint(kind="area_cell", description=f"Area cell {cell}")
        res = h3_resolution(cell)
        return Footprint(
            kind="area_cell",
            description=f"Area cell {cell} (H3 res {res}, about {size:.0f} km across)",
        )
    return Footprint(kind="area_cell", description="Area (no cell)")


def _f(e: Evidence, k: str, default: float = 0.0) -> float:
    return e.features.get(k, default)


def _g(x: float) -> str:
    """Compact number: 12 -> '12', 1.0 -> '1', 0.35 -> '0.3'."""
    return f"{x:.1f}".rstrip("0").rstrip(".") if x < 10 else f"{x:.0f}"


_SAFE_ENTITY = re.compile(r"[^A-Za-z0-9_:.-]")


def _entity(e: str) -> str:
    """An entity label restricted to a safe alphabet (it may originate from a scraped feed)."""
    return _SAFE_ENTITY.sub("", e)[:24]


def _summary(kind: AlertKind, group: Sequence[Evidence], lead: Evidence, cell: str | None) -> str:
    prov = ", ".join(lead.coverage.providers) or "no providers"
    cov = f"coverage {lead.coverage.confidence:.0%} ({prov})"
    pers = f"persistence {lead.persistence} (5-min steps spanned by anomalous reports)"
    ents = sorted({x for e in group for x in e.entities})
    if kind == AlertKind.GNSS_JAMMING and lead.detector == "cygnss":
        return (
            f"GNSS L1 noise seen from orbit (CYGNSS) in H3 {cell}: "
            f"{_f(lead, 'excess_db'):+.1f} dB above the open-ocean level"
            + (
                f", {_f(lead, 'delta_db'):+.1f} dB above its usual "
                f"{_f(lead, 'baseline_db'):+.1f} dB"
                if _f(lead, "delta_db")
                else ""
            )
            + f"; {_f(lead, 'n'):.0f} reflections from {_f(lead, 'n_sats'):.0f} satellites; "
            f"flagged on {lead.persistence} of the last observed days; {cov}."
        )
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
    if kind == AlertKind.AIS_SPOOFING:
        sigs = sorted({s for e in group for s in ("A1", "A2", "A3", "A4") if _f(e, s)})
        return (
            f"Possible AIS spoofing in H3 {cell}: {len(ents)} MMSI "
            f"({', '.join(ents[:5])}{'...' if len(ents) > 5 else ''}) with signals "
            f"{', '.join(sigs)}; {pers}; {cov}."
        )
    if kind == AlertKind.AIS_GAP:
        return (
            f"AIS dark gap in H3 {cell}: MMSI {', '.join(ents[:5])}"
            f"{'...' if len(ents) > 5 else ''} silent {_f(lead, 'gap_h'):.1f} h "
            f"({'still open' if _f(lead, 'open') else 'resumed'}) while "
            f"{_f(lead, 'covered_frac'):.0%} of hourly bins had good reception nearby; "
            f"{pers}; {cov}."
        )
    if kind == AlertKind.THERMAL:
        n = _f(lead, "n_detections")
        return (
            f"{n:.0f} fire detections in a normally quiet area (usual "
            f"\u2248{_g(_f(lead, 'baseline_mean_per_day'))}/day) in H3 {cell}; total heat output "
            f"{_f(lead, 'sum_frp_mw'):.0f} MW over {_f(lead, 'n_passes'):.0f} satellite passes; "
            f"{pers}; {cov}."
        )
    if kind == AlertKind.SPECTRUM_ANOMALY:
        parts = [r.description for e in group for r in e.rules[:1]]
        return (
            f"Unexplained change in {len(group)} band indicator(s) over this area: "
            f"{'; '.join(parts)}; {pers}; {cov}. Experimental: it never names an emitter."
        )
    if kind in (AlertKind.MIL_DEPLOYMENT, AlertKind.MIL_SURGE, AlertKind.MIL_HIGH_INTEREST):
        parts = list(dict.fromkeys(r.description for e in group for r in e.rules[:1]))
        return f"{'; '.join(parts)}. Seen from ADS-B only; {cov}."
    if kind == AlertKind.MIL_BUILDUP:
        hits = [r.description for r in lead.rules if r.passed]
        return (
            f"{len(hits)} independent build-up indicators: {'; '.join(hits)}. Indicator checklist "
            f"only: it does not predict a target or a strike; {cov}."
        )
    return f"{kind} in H3 {cell}: {len(group)} evidence items; {pers}; {cov}."


_TITLES = {
    AlertKind.GNSS_JAMMING: "GNSS jamming",
    AlertKind.GNSS_SPOOFING: "GNSS spoofing",
    AlertKind.MIL_ORBIT: "Military orbit",
    AlertKind.COMPOSITE: "Tanker orbits near GNSS jamming",
    AlertKind.AIS_SPOOFING: "AIS spoofing",
    AlertKind.AIS_GAP: "AIS dark gap",
    AlertKind.THERMAL: "Unusual fire activity",
    AlertKind.OUTAGE: "Connectivity or power outage",
    AlertKind.SPECTRUM_ANOMALY: "Spectrum anomaly",
    AlertKind.MIL_DEPLOYMENT: "Military deployment wave",
    AlertKind.MIL_SURGE: "Military activity surge",
    AlertKind.MIL_HIGH_INTEREST: "High-interest military aircraft",
    AlertKind.MIL_BUILDUP: "Possible military build-up",
}


def _union_bands(group: Sequence[Evidence]) -> list[Band]:
    """Ordered union of the Evidence bands; a real band supersedes NONE (context)."""
    out: list[Band] = list(dict.fromkeys(b for e in group for b in e.bands))
    real: list[Band] = [b for b in out if b != Band.NONE]
    return real or out


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
    notices: Sequence[GeoEvent] | None = None,
    context_events: Sequence[GeoEvent] | None = None,
) -> list[Alert]:
    """Return new/updated/expired alerts (caller upserts them).

    `notices`: active area notices (ADR-005) used only for corroboration.
    `context_events`: weather, space_weather, imagery_footprint and overpass events (ADR-007); they
    only lower confidence and add context notes, never corroborate.
    """
    c = _cfg(cfg)
    grades = source_grades or {}
    ctx_events = list(context_events or [])
    gated = [
        e
        for e in evidence
        if e.kind != AlertKind.WARNING_AREA  # context-only: never opens an alert
        and e.persistence >= required_persistence(e.kind, c)
    ]
    xs = cross_source(gated, gated, notices or [], c["corroboration"])
    fam_idx = family_index(gated, notices or [], c["source_family"])
    conn_idx = _connector_index(gated, notices or [])
    groups: dict[tuple[AlertKind, str | None], list[Evidence]] = defaultdict(list)
    for e in gated:
        groups[(e.kind, e.h3_cell)].append(e)
    for j, orbits in _composites(gated, c):
        groups[(AlertKind.COMPOSITE, j.h3_cell)] = [j, *orbits]

    by_key = {(a.kind, a.h3_cell): a for a in open_alerts if a.status in ACTIVE}
    # dismissed alerts own their episode (anti-chattering, ISA-18.2): most recent per kind+cell
    dismissed: dict[tuple[AlertKind, str | None], Alert] = {}
    for a in sorted(open_alerts, key=lambda a: a.last_seen):
        if a.status == AlertStatus.DISMISSED:
            dismissed[(a.kind, a.h3_cell)] = a
    clear_gap = timedelta(minutes=c["expire_after_min"])
    max_episode = timedelta(hours=float(c["dismiss_max_episode_h"]))
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
            if kind == AlertKind.SPECTRUM_ANOMALY and len({b for e in group for b in e.bands}) < 2:
                # ADR-008 s4: one experimental band alone is never more than medium
                sev = min(sev, Severity.MEDIUM, key=_SEV_ORDER.index)
            conf = lead.coverage.confidence
        if control:
            sev = Severity.INFO
        prev = by_key.get((kind, cell))
        d = dismissed.get((kind, cell))
        if (
            prev is None
            and d is not None
            and min(e.window_end for e in group) - d.last_seen <= clear_gap
            and max(e.window_end for e in group) - d.first_seen < max_episode
            and _SEV_ORDER.index(sev) <= _SEV_ORDER.index(d.severity)
        ):
            # same episode still running: attach evidence, keep the analyst's DISMISSED status
            ids = list(dict.fromkeys([*d.evidence_ids, *(e.id for e in group)]))
            alert = d.model_copy(
                update={
                    "evidence_ids": ids[-c["max_evidence_ids"] :],
                    "last_seen": max(d.last_seen, max(e.window_end for e in group)),
                }
            )
            touched.add(alert.id)
            out.append(alert)
            continue
        in_group = {e.id for e in group}
        uniq: dict[tuple[str, str], Corroboration] = {}
        for e in group:
            for x in xs.get(e.id, []):
                if x.evidence_id not in in_group:
                    uniq.setdefault((x.evidence_id, x.relation), x)
        corr = sorted(uniq.values(), key=lambda x: (x.relation != "supports", x.evidence_id))
        fam_own = {family_of(r.source, c["source_family"]) for e in group for r in e.records}
        fam_sup: set[str] = set()
        for x in corr:
            if x.relation == "supports":
                fam_sup |= fam_idx.get(x.evidence_id, set())
        families = independent_families(fam_own | fam_sup)
        status_c: CorrStatus = (
            "contradicted"
            if any(x.relation == "contradicts" for x in corr)
            else ("corroborated" if len(families) >= 2 else "uncorroborated")
        )
        adm = grade(
            group,
            corr,
            grades,
            required_persistence=required_persistence(lead.kind, c),
            strong_score=c["strong_score"],
            analyst_confirmed=prev is not None and prev.status == AlertStatus.CONFIRMED,
            families=families,
            same_family_support=bool(fam_sup & fam_own),
        )
        cf, cnotes, cgaps = confounders.assess(lead, ctx_events, c["confounders"])
        conf *= cf
        corr = [
            *corr,
            *cnotes,
            *observability.observability(lead, ctx_events, now, c["observability"]),
        ]
        sources = _alert_sources(group, corr, conn_idx, adm, grades, c["source_family"])
        footprint = _footprint(kind, group, cell)
        ev_ids = [e.id for e in sorted(group, key=lambda e: e.id)]
        first = min(e.window_start for e in group)
        last = max(e.window_end for e in group)
        summary = _summary(kind, group, lead, cell) + f" Admiralty {adm}."
        if len(families) > 1:
            summary += f" Independent source families: {', '.join(sorted(families))}."
        if cf < 1.0:
            summary += f" Confidence x{cf:.2f}: " + "; ".join(n.note for n in cnotes) + "."
        if control:
            summary += " [control AOI: calibration only, severity capped at info]"
        bands = _union_bands(group)
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
                    "gaps": cgaps,
                    "sources": sources,
                    "footprint": footprint,
                    "bands": list(dict.fromkeys([*prev.bands, *bands])),
                    "last_seen": max(prev.last_seen, last),
                    "lat": lead.lat,
                    "lon": lead.lon,
                }
            )
        else:
            alert = Alert(
                id=alert_id(str(kind), cell, first, aoi),
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
                gaps=cgaps,
                sources=sources,
                footprint=footprint,
                bands=bands,
                recommended_actions=actions_for(kind, playbook),
                status=AlertStatus.NEW,
                first_seen=first,
                last_seen=last,
            )
        touched.add(alert.id)
        out.append(alert)

    horizon = now - timedelta(minutes=c["expire_after_min"])
    for a in open_alerts:
        if a.status == AlertStatus.NEW and a.id not in touched and a.last_seen < horizon:
            out.append(a.model_copy(update={"status": AlertStatus.EXPIRED}))
    return out


def apply_evidence_update(
    current: Alert | None,
    update: Alert,
    max_evidence_ids: int = DEFAULTS["max_evidence_ids"],
    expire_before: datetime | None = None,
) -> Alert:
    """Merge a fusion result into the alert as currently stored (re-read just before upsert).

    Only EVIDENCE_FIELDS are taken from `update`; analyst-owned fields (status, title, actions,
    first_seen) keep the stored value. The one status transition the pipeline may make is
    NEW -> EXPIRED, and only if the *re-read* alert is still NEW and still stale: its last_seen is
    < `expire_before` (the staleness horizon) and not newer than the snapshot fusion expired. An
    expiry never copies the snapshot's evidence fields over a fresher stored alert.

    Dismissal race: if the analyst dismissed the alert between `fuse` and this re-read and the
    update carries a *higher* severity than the dismissed alert, the escalation is not dropped
    (docs/detectors.md, fusion rule 8): a new NEW alert is returned (id from the update's
    last_seen, so the dismissed alert keeps its id, status and episode) holding the evidence the
    dismissed alert has not seen. Same or lower severity -> episode bookkeeping only.
    """
    if current is None:
        return update
    if (
        current.status == AlertStatus.DISMISSED
        and update.status != AlertStatus.EXPIRED
        and _SEV_ORDER.index(update.severity) > _SEV_ORDER.index(current.severity)
    ):
        fresh = [i for i in update.evidence_ids if i not in set(current.evidence_ids)]
        return update.model_copy(
            update={
                "id": alert_id(str(update.kind), update.h3_cell, update.last_seen, update.aoi),
                "status": AlertStatus.NEW,
                "first_seen": update.last_seen,
                "evidence_ids": (fresh or list(update.evidence_ids))[-max_evidence_ids:],
            }
        )
    if current.status == AlertStatus.DISMISSED:  # episode bookkeeping only
        ids = list(dict.fromkeys([*current.evidence_ids, *update.evidence_ids]))
        return current.model_copy(
            update={
                "evidence_ids": ids[-max_evidence_ids:],
                "last_seen": max(current.last_seen, update.last_seen),
            }
        )
    if update.status == AlertStatus.EXPIRED:
        stale = current.last_seen <= update.last_seen and (
            expire_before is None or current.last_seen < expire_before
        )
        if current.status == AlertStatus.NEW and stale:
            return current.model_copy(update={"status": AlertStatus.EXPIRED})
        return current
    fields: dict[str, Any] = {f: getattr(update, f) for f in EVIDENCE_FIELDS}
    fields["evidence_ids"] = list(dict.fromkeys([*current.evidence_ids, *update.evidence_ids]))[
        -max_evidence_ids:
    ]
    fields["last_seen"] = max(current.last_seen, update.last_seen)
    return current.model_copy(update=fields)
