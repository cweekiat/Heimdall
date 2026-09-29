"""Admiralty (NATO AJP-2.1) grading: source reliability A-F x information credibility 1-6.

Pure functions. Choices (documented in docs/detectors.md):

* **Reliability = best grade among the sources that contributed observations to the Evidence.**
  The aggregators (adsb.lol, adsb.fi, airplanes.live, OpenSky) re-publish the *same* broadcast,
  so a finding is as reliable as the most reliable source that independently shows it; adding a
  weaker mirror must not downgrade it (worst-case would punish extra coverage). Unknown sources
  grade F; no sources -> F.
* **Credibility** from the number of *independent domains* that support the alert:
  1 = analyst-confirmed or >= 3 domains; 2 = 2 domains; 3 = single domain, strong
  (lead score >= strong_score) AND persistent (persistence >= required windows);
  4 = single domain otherwise; 5 = contradicted by another domain; 6 = no evidence (cannot judge).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from heimdall.schemas import (
    Admiralty,
    AlertKind,
    Corroboration,
    Domain,
    Evidence,
    SourceReliability,
)

KIND_DOMAIN: dict[AlertKind, Domain] = {
    AlertKind.GNSS_JAMMING: Domain.AIR,
    AlertKind.GNSS_SPOOFING: Domain.AIR,
    AlertKind.MIL_ORBIT: Domain.AIR,
    AlertKind.COMPOSITE: Domain.AIR,
    AlertKind.AIS_SPOOFING: Domain.SEA,
    AlertKind.AIS_GAP: Domain.SEA,
    AlertKind.WARNING_AREA: Domain.SEA,
    AlertKind.THERMAL: Domain.SPACE,
    AlertKind.OUTAGE: Domain.CYBER,
    AlertKind.EVENT_SURGE: Domain.EVENTS,
}

# provider names used in observations -> keys in config/sources.yaml
PROVIDER_ALIASES = {"history": "adsb_lol_history"}

_ORDER = "ABCDEF"


def source_key(provider: str) -> str:
    """'adsb.lol' -> 'adsb_lol', 'airplanes.live' -> 'airplanes_live', 'history' -> alias."""
    k = provider.strip().lower().replace(".", "_").replace("-", "_")
    return PROVIDER_ALIASES.get(k, k)


def grades_from_sources(sources_cfg: Mapping[str, object]) -> dict[str, str]:
    """Extract {source_key: 'A'..'F'} from the parsed sources.yaml mapping."""
    srcs = sources_cfg.get("sources", sources_cfg)
    out: dict[str, str] = {}
    if isinstance(srcs, Mapping):
        for k, v in srcs.items():
            if isinstance(v, Mapping) and isinstance(v.get("reliability"), str):
                out[str(k)] = str(v["reliability"]).upper()
    return out


def reliability(providers: Iterable[str], grades: Mapping[str, str]) -> SourceReliability:
    best = "F"
    for p in providers:
        g = grades.get(source_key(p), "F")
        if g in _ORDER and _ORDER.index(g) < _ORDER.index(best):
            best = g
    return SourceReliability(best)


def credibility(
    domains: set[Domain],
    *,
    has_evidence: bool = True,
    strong: bool = False,
    persistent: bool = False,
    contradicted: bool = False,
    analyst_confirmed: bool = False,
) -> int:
    if analyst_confirmed:
        return 1
    if not has_evidence:
        return 6
    if contradicted:
        return 5
    if len(domains) >= 3:
        return 1
    if len(domains) == 2:
        return 2
    return 3 if (strong and persistent) else 4


def grade(
    evidence: Sequence[Evidence],
    corroborations: Sequence[Corroboration],
    grades: Mapping[str, str],
    *,
    required_persistence: int,
    strong_score: float,
    analyst_confirmed: bool = False,
) -> Admiralty:
    """Admiralty grade for an alert built from `evidence` (+ cross-domain corroborations)."""
    providers = {p for e in evidence for p in e.coverage.providers}
    domains = {KIND_DOMAIN.get(e.kind, Domain.AIR) for e in evidence}
    domains |= {c.domain for c in corroborations if c.relation == "supports"}
    contradicted = any(c.relation == "contradicts" for c in corroborations)
    lead = max(evidence, key=lambda e: e.score, default=None)
    return Admiralty(
        reliability=reliability(providers, grades),
        credibility=credibility(
            domains,
            has_evidence=lead is not None,
            strong=lead is not None and lead.score >= strong_score,
            persistent=lead is not None and lead.persistence >= required_persistence,
            contradicted=contradicted,
            analyst_confirmed=analyst_confirmed,
        ),
    )
