"""Admiralty (NATO AJP-2.1) grading: source reliability A-F x information credibility 1-6.

Pure functions. Choices (documented in docs/detectors.md):

* **Reliability = best grade among the sources whose own reports show the anomaly**, i.e. the
  `source` of the Evidence's `records` (detectors emit one RecordRef per anomaly-showing
  provider). Providers that merely covered the area (`coverage.providers`) do not count.
  The aggregators (adsb.lol, adsb.fi, OpenSky) re-publish the *same* broadcast,
  so a finding is as reliable as the most reliable source that independently shows it; adding a
  weaker mirror must not downgrade it (worst-case would punish extra coverage). Unknown sources
  grade F; no sources -> F.
* **Credibility** from the number of *independent source families* that support the alert
  (ADR-005 section C; `fusion.source_family`: adsb, ais, notice, and ADR-007 thermal;
  the `context` family is never counted). Jamming and a tanker
  orbit are both `air` but one ADS-B feed, so domains would overcount; a NOTAM is an independent
  publisher. 1 = analyst-confirmed or >= 3 families; 2 = 2 families; 3 = one family, strong
  (lead score >= strong_score) AND persistent (persistence >= required windows), OR one family
  with same-family support (e.g. a tanker orbit near jamming); 4 = one family otherwise;
  5 = contradicted; 6 = no evidence (cannot judge). When `families` is not given (legacy callers)
  the count is over *domains* as before.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence

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

#: family of weather / space weather / orbit / imagery-catalogue sources (ADR-007 s.5-6). It says
#: "can this be explained / checked", never "an independent source saw it", so it is never counted.
CONTEXT_FAMILY = "context"


def independent_families(families: Iterable[str]) -> set[str]:
    """`families` without the non-counting `context` family."""
    return {f for f in families if f != CONTEXT_FAMILY}


def source_key(provider: str) -> str:
    """'adsb.lol' -> 'adsb_lol', 'history' -> alias."""
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
    domains: Collection[object],
    *,
    same_family_support: bool = False,
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
    return 3 if ((strong and persistent) or same_family_support) else 4


def grade(
    evidence: Sequence[Evidence],
    corroborations: Sequence[Corroboration],
    grades: Mapping[str, str],
    *,
    required_persistence: int,
    strong_score: float,
    analyst_confirmed: bool = False,
    families: Collection[str] | None = None,
    same_family_support: bool = False,
) -> Admiralty:
    """Admiralty grade for an alert built from `evidence` (+ corroborations).

    `families`: independent source families supporting the alert (own + supporting); when None the
    legacy domain count is used. `same_family_support`: a supporting claim came from a family
    already counted (credibility 3 instead of 4).
    """
    # only providers whose own reports show the anomaly: detectors emit one RecordRef per such
    # provider; coverage.providers (everyone who saw the area) is NOT used for reliability
    providers = {r.source for e in evidence for r in e.records}
    domains = {KIND_DOMAIN.get(e.kind, Domain.AIR) for e in evidence}
    domains |= {c.domain for c in corroborations if c.relation == "supports"}
    contradicted = any(c.relation == "contradicts" for c in corroborations)
    lead = max(evidence, key=lambda e: e.score, default=None)
    return Admiralty(
        reliability=reliability(providers, grades),
        credibility=credibility(
            domains if families is None else independent_families(families),
            same_family_support=same_family_support,
            has_evidence=lead is not None,
            strong=lead is not None and lead.score >= strong_score,
            persistent=lead is not None and lead.persistence >= required_persistence,
            contradicted=contradicted,
            analyst_confirmed=analyst_confirmed,
        ),
    )
