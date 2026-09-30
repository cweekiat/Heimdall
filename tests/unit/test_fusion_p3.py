"""P3 fusion (task 022b): corroboration pairs, confounders, context families, summaries."""

from __future__ import annotations

from datetime import timedelta

from heimdall.config import AOI
from heimdall.detect.outage import detect as detect_outage
from heimdall.fuse import admiralty
from heimdall.fuse.corroborate import cross_source, family_of
from heimdall.fuse.fusion import fuse
from heimdall.geo import h3_cell
from heimdall.schemas import AlertKind, Coverage, Evidence, RecordRef, Severity
from tests.synthetic import context as ctx
from tests.synthetic import outage as outsyn
from tests.synthetic.generators import T0
from tests.unit.test_warning_area import events

NOW = T0 + timedelta(minutes=30)
GRADES = {"adsb_lol": "B", "ioda": "B", "black_marble": "A", "firms": "A"}
JAM_LAT, JAM_LON = 12.5, 113.5  # inside the notices of tests.unit.test_warning_area


def mk(
    kind: AlertKind,
    ident: str,
    lat: float,
    lon: float,
    src: str = "adsb.lol",
    res: int = 4,
    detector: str = "t",
    days_back: int = 0,
    features: dict[str, float] | None = None,
) -> Evidence:
    end = NOW
    return Evidence(
        id=ident,
        kind=kind,
        detector=detector,
        detector_version="t-1",
        window_start=end - timedelta(minutes=30, days=days_back),
        window_end=end,
        h3_cell=h3_cell(lat, lon, res),
        h3_res=res,
        lat=lat,
        lon=lon,
        score=0.8,
        persistence=2,
        features=features or {},
        records=[RecordRef(source=src, id=f"{ident}-r")],
        coverage=Coverage(n_observed=10, n_quality=10, providers=[src], confidence=0.9),
        created_at=end,
    )


def lights(ident: str, lat: float, lon: float) -> Evidence:
    return mk(
        AlertKind.OUTAGE,
        ident,
        lat,
        lon,
        src="black_marble",
        detector="night_lights",
        days_back=2,
        features={"worst_ratio": 0.2, "n_dropped_nights": 3, "imagery_lag_days": 2},
    )


def sup(cs: list, kinds: bool = False) -> set[str]:  # type: ignore[type-arg]
    return {c.evidence_id for c in cs if c.relation == "supports"}


def one(evs: list[Evidence], **kw):  # type: ignore[no-untyped-def]
    return {a.kind: a for a in fuse(evs, [], NOW, None, {}, GRADES, **kw)}


# ------------------------------------------------------------------ corroborate pairs


def test_thermal_and_jamming_support_each_other() -> None:
    jam = mk(AlertKind.GNSS_JAMMING, "j1", 25.0, 96.0)
    hot = mk(AlertKind.THERMAL, "t1", 25.2, 96.0, src="firms", res=6)  # ~22 km
    far = mk(AlertKind.THERMAL, "t2", 26.0, 96.0, src="firms", res=6)  # ~111 km
    out = cross_source([jam, hot, far], [jam, hot, far], [])
    assert sup(out["j1"]) == {"t1"} and sup(out["t1"]) == {"j1"} and sup(out["t2"]) == set()


def test_thermal_supported_and_outage_context_from_covering_notice() -> None:
    hot = mk(AlertKind.THERMAL, "t1", JAM_LAT, JAM_LON, src="firms", res=6)
    assert {"nga-exercise-1"} <= sup(cross_source([hot], [hot], events())["t1"])
    dark = mk(AlertKind.OUTAGE, "o1", JAM_LAT, JAM_LON, src="ioda")
    got = cross_source([dark], [dark], events())["o1"]
    assert sup(got) == set()
    assert "nga-exercise-1" in {c.evidence_id for c in got if c.relation == "context"}


def test_ioda_outage_and_night_lights_support_each_other() -> None:
    io = mk(AlertKind.OUTAGE, "o1", 21.9, 95.9, src="ioda")
    nl = lights("o2", 22.0, 95.9)  # ~11 km
    far = lights("o3", 23.5, 95.9)
    out = cross_source([io, nl, far], [io, nl, far], [])
    assert sup(out["o1"]) == {"o2"} and sup(out["o2"]) == {"o1"} and sup(out["o3"]) == set()
    io2 = mk(AlertKind.OUTAGE, "o4", 21.95, 95.9, src="ioda")  # two IODA outages: not independent
    assert sup(cross_source([io, io2], [io, io2], [])["o1"]) == set()


def test_outage_and_thermal_in_same_r4_support() -> None:
    io = mk(AlertKind.OUTAGE, "o1", 21.9, 95.9, src="ioda")
    hot = mk(AlertKind.THERMAL, "t1", 21.9, 95.9, src="firms", res=6)
    other = mk(AlertKind.THERMAL, "t2", 24.9, 95.9, src="firms", res=6)
    out = cross_source([io, hot, other], [io, hot, other], [])
    assert sup(out["o1"]) == {"t1"} and sup(out["t1"]) == {"o1"} and sup(out["t2"]) == set()


def test_p3_families_and_context_family_not_counted() -> None:
    assert family_of("firms") == "thermal" and family_of("black_marble") == "night_lights"
    assert family_of("ioda") == "ioda" and family_of("cloudflare_radar") == "cloudflare"
    for k in ("open_meteo", "swpc", "celestrak", "stac"):
        assert family_of(k) == "context"
    lead = mk(AlertKind.GNSS_JAMMING, "j", 25.0, 96.0)
    a = admiralty.grade(
        [lead], [], GRADES, required_persistence=2, strong_score=0.6, families={"adsb", "context"}
    )
    b = admiralty.grade(
        [lead], [], GRADES, required_persistence=2, strong_score=0.6, families={"adsb"}
    )
    assert a.credibility == b.credibility


# ------------------------------------------------------------------ fusion


def test_ioda_plus_night_lights_is_corroborated_and_improves_credibility() -> None:
    io = mk(
        AlertKind.OUTAGE,
        "o1",
        21.9,
        95.9,
        src="ioda",
        features={"n_datasources": 2, "duration_h": 3, "min_ratio": 0.2},
    )
    nl = lights("o2", 22.1, 95.9)
    assert io.h3_cell != nl.h3_cell
    alone = next(a for a in fuse([io], [], NOW, None, {}, GRADES) if a.evidence_ids == ["o1"])
    both = next(a for a in fuse([io, nl], [], NOW, None, {}, GRADES) if a.evidence_ids == ["o1"])
    assert alone.corroboration_status == "uncorroborated"
    assert both.corroboration_status == "corroborated"
    assert both.admiralty.credibility == alone.admiralty.credibility - 1
    assert {s.family for s in both.sources} == {"ioda", "night_lights"}


def test_stac_and_overpass_context_never_corroborate() -> None:
    io = mk(AlertKind.OUTAGE, "o1", 5.0, 105.0, src="ioda")
    fp = ctx.footprint(NOW - timedelta(hours=3))
    op = ctx.overpass(NOW + timedelta(hours=5))
    alone = fuse([io], [], NOW, None, {}, GRADES)[0]
    (a,) = fuse([io], [], NOW, None, {}, GRADES, context_events=[fp, op])
    assert a.corroboration_status == "uncorroborated"
    assert a.admiralty == alone.admiralty and a.confidence == alone.confidence
    ctxn = [c for c in a.corroborations if c.relation == "context"]
    assert len(ctxn) >= 2 and not [c for c in a.corroborations if c.relation == "supports"]


def test_kp8_halves_confidence_keeps_severity_and_carries_numeric_factor() -> None:
    jam = mk(AlertKind.GNSS_JAMMING, "j1", 25.0, 96.0, features={"bad": 4, "total": 12})
    base = fuse([jam], [], NOW, None, {}, GRADES)[0]
    (a,) = fuse(
        [jam],
        [],
        NOW,
        None,
        {},
        GRADES,
        context_events=[ctx.space_weather(8.0, ts=NOW - timedelta(hours=1), g=4)],
    )
    assert a.severity == base.severity and a.admiralty == base.admiralty
    assert abs(a.confidence - base.confidence * 0.5) < 1e-3
    (n,) = [c for c in a.corroborations if c.relation == "context"]
    assert n.factor == 0.5 and "Kp 8.0" in n.note
    assert a.gaps == []
    # no space weather at all: factor 1 and a gap, never a penalty
    assert base.confidence == 0.9 and base.gaps == ["space weather context unavailable (lag)"]


def test_thermal_near_jamming_supports_and_control_cap_holds() -> None:
    jam = mk(AlertKind.GNSS_JAMMING, "j1", 55.1, 20.9)
    hot = mk(
        AlertKind.THERMAL,
        "t1",
        55.3,
        20.9,
        src="firms",
        res=6,
        features={
            "n_detections": 12,
            "baseline_mean_per_day": 1.0,
            "sum_frp_mw": 80,
            "n_passes": 3,
        },
    )
    alerts = one([jam, hot])
    t = alerts[AlertKind.THERMAL]
    assert t.corroboration_status == "corroborated" and t.admiralty.credibility == 2
    assert "12 fire detections in a normally quiet area (usual ≈1/day)" in t.summary
    ctl = one([jam, hot], control=True)
    assert all(a.severity == Severity.INFO for a in ctl.values())


def test_injection_text_never_reaches_summary_or_notes() -> None:
    aoi = AOI(name="myanmar", lat=20.5, lon=96.5, radius_nm=250)
    evs = detect_outage([*outsyn.ioda_drop(), outsyn.annotation()], {}, outsyn.NOW, aoi=aoi)
    (o,) = evs
    o = o.model_copy(update={"persistence": 2})
    (a,) = fuse([o], [], outsyn.NOW, None, {}, GRADES, context_events=[ctx.footprint(outsyn.NOW)])
    dump = a.model_dump_json()
    assert outsyn.INJECTION not in dump and ctx.INJECTION not in dump
    assert a.summary.startswith("Internet outage")
