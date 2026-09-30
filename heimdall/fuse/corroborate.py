"""Cross-source corroboration (ADR-005 section D). Pure: no I/O, no notice text.

`cross_source(gated, context, notices, cfg)` maps each gated Evidence id to the Corroborations that
other sources give it:

* jamming / GNSS spoofing  <- active notice (NAVWARN/NOTAM) of category military_exercise,
  live_firing, missile_rocket or gnss_interference that covers the point (or lies within
  `proximity_km` of it) and overlaps the window (+- `time_slack_h`);
* jamming <-> AIS spoofing within `jam_ais_km` (both directions);
* mil orbit <- covering notice, or gated jamming within `jam_orbit_km` (same ADS-B feed: fusion
  sees it as *same-family* support);
* thermal <-> jamming within `thermal_jam_km`; thermal <- covering notice (as jamming);
  needs windows overlapping within `time_slack_h`;
* `context`: other-kind Evidence in `context` in the same H3 cell that
  is not already support. Context kinds (weather, imagery, overpass) never count as families.

Notes are built only from `attrs.ref` (re-sanitised), source and category: notice `title`/`text`
are untrusted and never read. Absence of a notice is never a contradiction.

`family_index` / `family_of` give the source family (config `fusion.source_family`) that fusion
uses to count *independent* support: two claims from one family are one voice.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any, Literal

from heimdall.detect.warning_area import covering_cells, safe_ref
from heimdall.fuse.admiralty import KIND_DOMAIN, source_key
from heimdall.geo import h3_center, h3_parent, haversine_km
from heimdall.schemas import AlertKind, Corroboration, Domain, Evidence, GeoEvent

CORROBORATING_CATEGORIES = (
    "military_exercise",
    "live_firing",
    "missile_rocket",
    "gnss_interference",
)

DEFAULT_CORROBORATION: dict[str, Any] = {
    "proximity_km": 50,
    "jam_ais_km": 100,
    "jam_orbit_km": 100,
    "time_slack_h": 2,
    "thermal_jam_km": 50,  # prior, not backtested
}

DEFAULT_SOURCE_FAMILY: dict[str, str] = {
    "adsb_lol": "adsb",
    "adsb_fi": "adsb",
    "adsb_lol_history": "adsb",
    "opensky": "adsb",
    "gpsjam": "adsb",
    "aisstream": "ais",
    "nga_msi": "notice",
    "faa_nms_notam": "notice",
    "firms": "thermal",  # ADR-007 s.6
    "open_meteo": "context",
    "swpc": "context",
    "celestrak": "context",
    "stac": "context",
}


def family_of(source: str, source_family: Mapping[str, str] | None = None) -> str:
    """Family of a source/provider name; an unmapped source is its own family."""
    key = source_key(source)
    return (source_family or DEFAULT_SOURCE_FAMILY).get(key, f"src:{key}")


def family_index(
    evidence: Iterable[Evidence],
    notices: Iterable[GeoEvent],
    source_family: Mapping[str, str] | None = None,
) -> dict[str, set[str]]:
    """{evidence id or notice id: families of the sources that show it}."""
    idx: dict[str, set[str]] = defaultdict(set)
    for e in evidence:
        idx[e.id] |= {family_of(r.source, source_family) for r in e.records}
    for n in notices:
        idx[n.id].add(family_of(n.source, source_family))
    return idx


def _covers(e: Evidence, n: GeoEvent, prox_km: float) -> bool:
    """Notice covers the Evidence point: same cell, a covering cell centre or centroid nearby."""
    cells = covering_cells(n)
    if e.h3_cell and e.h3_cell in cells:
        return True
    if e.lat is None or e.lon is None:
        return False
    if (
        n.lat is not None
        and n.lon is not None
        and haversine_km(e.lat, e.lon, n.lat, n.lon) <= prox_km
    ):
        return True
    return any(haversine_km(e.lat, e.lon, *h3_center(c)) <= prox_km for c in cells)


def _overlaps(e: Evidence, n: GeoEvent, slack: timedelta) -> bool:
    if n.attrs.get("status", "active") != "active":
        return False
    if n.valid_from is not None and n.valid_from > e.window_end + slack:
        return False
    return n.valid_to is None or n.valid_to >= e.window_start - slack


def _span(e: Evidence) -> tuple[float, float]:
    f = e.features
    return (
        f.get("anomaly_first_ts", e.window_start.timestamp()),
        f.get("anomaly_last_ts", e.window_end.timestamp()),
    )


def _dist(a: Evidence, b: Evidence) -> float | None:
    if a.lat is None or a.lon is None or b.lat is None or b.lon is None:
        return None
    return haversine_km(a.lat, a.lon, b.lat, b.lon)


def _near_in_time(a: Evidence, b: Evidence, slack: timedelta) -> bool:
    return a.window_start <= b.window_end + slack and b.window_start <= a.window_end + slack


def _r4(e: Evidence) -> str | None:
    """The evidence's H3 r4 ancestor (None for absent, invalid or coarser-than-r4 cells)."""
    if not e.h3_cell:
        return None
    try:
        return h3_parent(e.h3_cell, 4)
    except Exception:  # synthetic ids or a cell coarser than r4
        return None


def _notice_corr(
    n: GeoEvent, relation: Literal["supports", "context"] = "supports"
) -> Corroboration:
    cat = str(n.attrs.get("category") or "other")
    kind = "NOTAM" if n.kind == "notam" else "navigational warning"
    note = (
        f"official notice declares GNSS interference in this area: {kind} {safe_ref(n)} ({n.source})"
        if cat == "gnss_interference"
        else f"active {kind} {safe_ref(n)} ({n.source}, {cat}) covers the area"
    )
    return Corroboration(domain=n.domain, evidence_id=n.id, relation=relation, note=note)


def _p3_supports(
    e: Evidence,
    jams: Sequence[Evidence],
    thermals: Sequence[Evidence],
    c: Mapping[str, Any],
    slack: timedelta,
) -> list[Corroboration]:
    """Thermal <-> jamming (ADR-007)."""
    out: list[Corroboration] = []

    def add(o: Evidence, domain: Domain, note: str) -> None:
        out.append(Corroboration(domain=domain, evidence_id=o.id, relation="supports", note=note))

    if e.kind == AlertKind.THERMAL:
        for j in jams:
            d = _dist(e, j)
            if d is not None and d <= float(c["thermal_jam_km"]) and _near_in_time(e, j, slack):
                add(j, Domain.AIR, f"GNSS jamming evidence {d:.0f} km from the fire cluster")
    if e.kind == AlertKind.GNSS_JAMMING:
        for t in thermals:
            d = _dist(e, t)
            if d is not None and d <= float(c["thermal_jam_km"]) and _near_in_time(e, t, slack):
                add(t, Domain.SPACE, f"fire detections {d:.0f} km from the jamming area")
    return out


def cross_source(
    gated: Sequence[Evidence],
    context: Sequence[Evidence],
    notices: Sequence[GeoEvent],
    cfg: Mapping[str, Any] | None = None,
) -> dict[str, list[Corroboration]]:
    """Corroborations per gated Evidence id (supports first, then context), deduplicated."""
    c = {**DEFAULT_CORROBORATION, **(cfg or {})}
    prox, slack = float(c["proximity_km"]), timedelta(hours=float(c["time_slack_h"]))
    area = [
        n
        for n in notices
        if n.kind in ("nav_warning", "notam")
        and str(n.attrs.get("category") or "other") in CORROBORATING_CATEGORIES
    ]
    jams = [e for e in gated if e.kind == AlertKind.GNSS_JAMMING]
    ais_sp = [e for e in gated if e.kind == AlertKind.AIS_SPOOFING]
    thermals = [e for e in gated if e.kind == AlertKind.THERMAL]

    out: dict[str, list[Corroboration]] = {}
    for e in gated:
        supports: list[Corroboration] = []
        ctx: list[Corroboration] = []
        if e.kind in (
            AlertKind.GNSS_JAMMING,
            AlertKind.GNSS_SPOOFING,
            AlertKind.MIL_ORBIT,
            AlertKind.THERMAL,
        ):
            supports += [
                _notice_corr(n) for n in area if _overlaps(e, n, slack) and _covers(e, n, prox)
            ]
        if e.kind == AlertKind.GNSS_JAMMING:
            for o in ais_sp:
                d = _dist(e, o)
                if d is not None and d <= float(c["jam_ais_km"]):
                    supports.append(
                        Corroboration(
                            domain=Domain.SEA,
                            evidence_id=o.id,
                            relation="supports",
                            note=f"AIS spoofing evidence {d:.0f} km away",
                        )
                    )
        if e.kind == AlertKind.AIS_SPOOFING:
            for j in jams:
                d = _dist(e, j)
                if d is not None and d <= float(c["jam_ais_km"]):
                    supports.append(
                        Corroboration(
                            domain=Domain.AIR,
                            evidence_id=j.id,
                            relation="supports",
                            note=f"GNSS jamming evidence {d:.0f} km away",
                        )
                    )
        if e.kind == AlertKind.MIL_ORBIT:
            for j in jams:
                d = _dist(e, j)
                if d is not None and d <= float(c["jam_orbit_km"]):
                    supports.append(
                        Corroboration(
                            domain=Domain.AIR,
                            evidence_id=j.id,
                            relation="supports",
                            note=f"GNSS jamming evidence {d:.0f} km away (same ADS-B feed)",
                        )
                    )
        supports += _p3_supports(e, jams, thermals, c, slack)
        seen = {x.evidence_id for x in supports}
        for o in context:
            if o.id != e.id and o.kind != e.kind and o.h3_cell == e.h3_cell and o.id not in seen:
                if o.kind == AlertKind.WARNING_AREA:
                    continue
                ctx.append(
                    Corroboration(
                        domain=KIND_DOMAIN.get(o.kind, Domain.AIR),
                        evidence_id=o.id,
                        relation="context",
                        note=f"co-located {o.kind} evidence in the same cell",
                    )
                )
        uniq: dict[tuple[str, str], Corroboration] = {}
        for x in [*supports, *ctx]:
            uniq.setdefault((x.evidence_id, x.relation), x)
        out[e.id] = list(uniq.values())
    return out
