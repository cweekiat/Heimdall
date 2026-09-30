"""Cross-source corroboration (ADR-005 section D). Pure: no I/O, no notice text.

`cross_source(gated, context, notices, cfg)` maps each gated Evidence id to the Corroborations that
other sources give it:

* jamming / GNSS spoofing  <- active notice (NAVWARN/NOTAM) of category military_exercise,
  live_firing, missile_rocket or gnss_interference that covers the point (or lies within
  `proximity_km` of it) and overlaps the window (+- `time_slack_h`);
* jamming <-> AIS spoofing within `jam_ais_km` (both directions);
* mil orbit <- covering notice, or gated jamming within `jam_orbit_km` (same ADS-B feed: fusion
  sees it as *same-family* support);
* AIS gap <- GFW gap of the same MMSI overlapping the gap, or a SAR detection within `sar_km` of the
  last fix during the gap; GFW loitering/encounter of the same MMSI -> `context`.
* `context`: other-kind Evidence in `context` in the same H3 cell that is not already support.

Notes are built only from `attrs.ref` (re-sanitised), source and category: notice `title`/`text`
are untrusted and never read. Absence of a notice is never a contradiction.

`family_index` / `family_of` give the source family (config `fusion.source_family`) that fusion
uses to count *independent* support: two claims from one family are one voice.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any

from heimdall.detect.warning_area import covering_cells, safe_ref
from heimdall.fuse.admiralty import KIND_DOMAIN, source_key
from heimdall.geo import h3_center, haversine_km
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
    "sar_km": 10,
    "time_slack_h": 2,
}

DEFAULT_SOURCE_FAMILY: dict[str, str] = {
    "adsb_lol": "adsb",
    "adsb_fi": "adsb",
    "airplanes_live": "adsb",
    "adsb_lol_history": "adsb",
    "opensky": "adsb",
    "gpsjam": "adsb",
    "aisstream": "ais",
    "gfw": "gfw",
    "gfw_sar": "sar",
    "nga_msi": "notice",
    "china_msa": "notice",
    "faa_nms_notam": "notice",
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


def _notice_corr(n: GeoEvent) -> Corroboration:
    cat = str(n.attrs.get("category") or "other")
    kind = "NOTAM" if n.kind == "notam" else "navigational warning"
    return Corroboration(
        domain=n.domain,
        evidence_id=n.id,
        relation="supports",
        note=f"active {kind} {safe_ref(n)} ({n.source}, {cat}) covers the area",
    )


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
    by_mmsi: dict[str, list[GeoEvent]] = defaultdict(list)
    for n in notices:
        if (
            n.kind in ("gfw_gap", "gfw_loitering", "gfw_encounter")
            and n.attrs.get("mmsi") is not None
        ):
            by_mmsi[str(n.attrs["mmsi"])].append(n)
    sars = [n for n in notices if n.kind == "sar_detection"]
    jams = [e for e in gated if e.kind == AlertKind.GNSS_JAMMING]
    ais_sp = [e for e in gated if e.kind == AlertKind.AIS_SPOOFING]

    out: dict[str, list[Corroboration]] = {}
    for e in gated:
        supports: list[Corroboration] = []
        ctx: list[Corroboration] = []
        if e.kind in (AlertKind.GNSS_JAMMING, AlertKind.GNSS_SPOOFING, AlertKind.MIL_ORBIT):
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
        if e.kind == AlertKind.AIS_GAP and e.entities:
            t0, t1 = _span(e)
            s = slack.total_seconds()
            for n in by_mmsi.get(e.entities[0], []):
                a = (n.valid_from or n.ts).timestamp()
                b = (n.valid_to or n.valid_from or n.ts).timestamp()
                if n.kind == "gfw_gap" and a <= t1 + s and b >= t0 - s:
                    supports.append(
                        Corroboration(
                            domain=Domain.SEA,
                            evidence_id=n.id,
                            relation="supports",
                            note=f"{n.source} reports an AIS gap for the same MMSI",
                        )
                    )
                elif n.kind in ("gfw_loitering", "gfw_encounter"):
                    ctx.append(
                        Corroboration(
                            domain=Domain.SEA,
                            evidence_id=n.id,
                            relation="context",
                            note=f"{n.source} {n.kind[4:]} event for the same MMSI",
                        )
                    )
            if e.lat is not None and e.lon is not None:
                for n in sars:
                    if n.lat is None or n.lon is None:
                        continue
                    t = n.ts.timestamp()
                    if t0 - s <= t <= t1 + s and haversine_km(e.lat, e.lon, n.lat, n.lon) <= float(
                        c["sar_km"]
                    ):
                        supports.append(
                            Corroboration(
                                domain=Domain.SPACE,
                                evidence_id=n.id,
                                relation="supports",
                                note=f"{n.source} SAR detection near the last fix during the gap",
                            )
                        )
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
