"""Task 038: CYGNSS L1 noise-floor connector and detector (offline, synthetic arrays)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import h3
import numpy as np
import pytest

from heimdall.detect import cygnss as det
from heimdall.fuse.corroborate import cross_source
from heimdall.fuse.fusion import fuse
from heimdall.ingest.cygnss import (
    CygnssConnector,
    day_events,
    merge_day,
    parse_cmr,
    parse_title,
    reduce_arrays,
)
from heimdall.schemas import (
    AlertKind,
    Band,
    Coverage,
    Evidence,
    GeoEvent,
    RecordRef,
    Severity,
)

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
TITLE = "cyg07.ddmi.s20260927-000000-e20260927-235959.l1.power-brcs.a32.d33"


@dataclass
class Aoi:
    name: str = "gulf"
    lat: float = 27.0
    lon: float = 51.5
    radius_nm: float = 200.0


def test_parse_title_rejects_unexpected_names() -> None:
    assert parse_title(TITLE) == ("cyg07", date(2026, 9, 27))
    for bad in (
        "../x",
        TITLE + "/..",
        "cyg09.ddmi.s20260927-000000-e20260927-235959.l1.power-brcs.a32.d33",
    ):
        assert parse_title(bad) is None


def test_parse_cmr_keeps_opendap_link_on_allowed_host() -> None:
    def entry(title: str, href: str) -> dict[str, object]:
        return {"title": title, "time_start": "2026-09-27T00:00:00Z", "links": [{"href": href}]}

    good = f"https://opendap.earthdata.nasa.gov/collections/C1/granules/{TITLE}"
    payload = {
        "feed": {
            "entry": [
                entry(TITLE, good),
                entry(TITLE, f"https://evil.example/collections/C1/granules/{TITLE}"),
                entry("bad title", good),
                entry(TITLE, good + "?x=1"),
            ]
        }
    }
    assert parse_cmr(payload, ("opendap.earthdata.nasa.gov",)) == [
        (TITLE, datetime(2026, 9, 27, tzinfo=UTC), good)
    ]


def _arrays(n: int = 4000) -> tuple[np.ndarray, ...]:
    """Open ocean far away (reference) + a jammed patch at 27N 51.5E (+6 dB, half rfi-flagged)."""
    rng = np.random.default_rng(1)
    lat = np.full((n, 4), -30.0) + rng.uniform(-1, 1, (n, 4))
    lon = np.full((n, 4), 200.0) + rng.uniform(-1, 1, (n, 4))  # 0-360 like CYGNSS
    nf = np.full((n, 4), 7000.0)
    q = np.zeros((n, 4), dtype=np.int32)
    lat[:100], lon[:100], nf[:100] = 27.0, 51.5, 7000.0 * 10**0.6
    q[:50] |= 131072
    q[100:110] = 256  # idle channel: dropped
    return lat, lon, nf, q


def test_reduce_normalises_to_open_ocean_and_bins_in_bbox() -> None:
    red = reduce_arrays(*_arrays(), bbox=(25, -40, 160, 40), h3_res=3)
    assert red is not None and red["ref"] == [7000.0] * 4
    cell = h3.latlng_to_cell(27.0, 51.5, 3)
    assert list(red["cells"]) == [cell]  # the -30/-160 reference ocean is outside the bbox
    n, db, n_ocean, n_rfi = red["cells"][cell]
    assert n == 400 and db == pytest.approx(6.0, abs=0.01) and n_ocean == 400 and n_rfi == 200


def test_reduce_needs_open_ocean_reference() -> None:
    lat, lon, nf, q = _arrays()
    q |= 1024  # everything over land
    assert reduce_arrays(lat, lon, nf, q) is None


def _red(sat: str, cells: dict[str, list[float]]) -> dict[str, object]:
    return {"sat": sat, "cells": cells}


def test_merge_day_and_baseline_in_events() -> None:
    c = h3.latlng_to_cell(27.0, 51.5, 3)
    today = merge_day(
        [_red("cyg01", {c: [100, 4.0, 90, 10]}), _red("cyg02", {c: [300, 2.0, 300, 0]})]
    )
    assert today[c].n == 400 and today[c].db == pytest.approx(2.5) and len(today[c].sats) == 2
    hist = [merge_day([_red("cyg01", {c: [50, v, 50, 0]})]) for v in (0.1, 0.2, 0.3)]
    (e,) = day_events(date(2026, 9, 27), today, hist, min_samples=20)
    a = e.attrs
    assert e.id == f"cygnss|2026-09-27|{c}" and e.kind == "l1_noise" and e.value == 2.5
    assert a["base_db"] == 0.2 and a["base_days"] == 3 and a["n_sats"] == 2
    assert a["ocean_frac"] == pytest.approx(0.975) and a["rfi_frac"] == 0.025


async def test_needs_key_makes_no_request(tmp_path: Path) -> None:
    c = CygnssConnector("cygnss", "https://cmr.invalid", token=None, cache_dir=tmp_path)
    assert c.health().status == "needs_key" and await c.poll_once() == []


def test_events_for_reads_cache_and_prune(tmp_path: Path) -> None:
    c = CygnssConnector("cygnss", "https://cmr.invalid", token="t", cache_dir=tmp_path)
    c.red_dir.mkdir(parents=True)
    cell = h3.latlng_to_cell(27.0, 51.5, 3)
    for d in range(10, 28):
        t = TITLE.replace("20260927", f"202609{d:02d}")
        c.cached(t).write_text(json.dumps(_red("cyg07", {cell: [60, 0.1 * (d == 27) * 30, 60, 0]})))
    (e,) = c.events_for(date(2026, 9, 27))
    assert e.value == pytest.approx(3.0) and e.attrs["base_days"] == 14
    c.prune(date(2026, 9, 30))  # keeps baseline_days + lookback + 3 = 21 days
    assert len(list(c.red_dir.glob("*.json"))) == 18


# ---------------------------------------------------------------- detector


def _ev(
    day: int, value: float, *, lat: float = 27.0, lon: float = 51.5, **attrs: object
) -> GeoEvent:
    cell = h3.latlng_to_cell(lat, lon, 3)
    a: dict[str, object] = {
        "h3": cell,
        "n": 150,
        "n_sats": 4,
        "ocean_frac": 1.0,
        "rfi_frac": 0.1,
        "base_db": None,
        "base_mad_db": None,
        "base_days": 0,
        "base_rfi_frac": None,
    }
    a.update(attrs)
    t = datetime(2026, 9, day, tzinfo=UTC)
    return GeoEvent(
        id=f"cygnss|2026-09-{day}|{cell}",
        ts=t,
        source="cygnss",
        domain="spectrum",
        kind="l1_noise",
        lat=lat,
        lon=lon,
        value=value,
        attrs=a,
    )


def test_level_rule_fires_on_open_ocean_with_persistence() -> None:
    evs = [_ev(26, 3.6), _ev(27, 3.7), _ev(25, 1.0)]
    (e,) = det.detect(evs, {}, NOW, aoi=Aoi())
    assert e.kind == AlertKind.GNSS_JAMMING and e.detector == "cygnss" and e.bands == [Band.L1_GNSS]
    assert e.persistence == 2 and e.h3_res == 3
    assert e.window_start == datetime(2026, 9, 27, tzinfo=UTC)
    rules = {r.rule: r for r in e.rules}
    assert rules["C1_noise_level"].passed and not rules["C2_change_vs_own_baseline"].passed
    assert e.coverage.confidence <= 0.55 and "short baseline: 0 days" in e.gaps
    assert "excess were below 2.5 dB" in e.counterfactual


def test_quiet_and_coastal_levels_do_not_fire() -> None:
    assert det.detect([_ev(27, 1.4)], {}, NOW, aoi=Aoi()) == []
    assert det.detect([_ev(27, 3.0, ocean_frac=0.5)], {}, NOW, aoi=Aoi()) == []  # land in beam
    (land,) = det.detect([_ev(27, 4.2, ocean_frac=0.1)], {}, NOW, aoi=Aoi())  # persistent jammer
    assert "land/coast threshold" in land.rules[0].description and land.rules[0].threshold == 3.5
    assert det.detect([_ev(27, 3.0, n=10)], {}, NOW, aoi=Aoi()) == []  # too few reflections
    assert det.detect([_ev(27, 4.0, lat=-20, lon=100)], {}, NOW, aoi=Aoi()) == []  # outside AOI
    assert det.detect([_ev(20, 4.0)], {}, NOW, aoi=Aoi()) == []  # older than max_age_days
    assert det.detect([_ev(26, 4.0), _ev(27, 1.0)], {}, NOW, aoi=Aoi()) == []  # latest quiet


def test_change_rule_on_land_vs_own_baseline() -> None:
    base = {"ocean_frac": 0.0, "base_db": 0.5, "base_mad_db": 0.1, "base_days": 14}
    (e,) = det.detect([_ev(27, 2.4, **base)], {}, NOW, aoi=Aoi())
    r = {x.rule: x for x in e.rules}["C2_change_vs_own_baseline"]
    assert r.passed and r.value == pytest.approx(1.9) and r.baseline == 0.5
    assert e.persistence == 1  # fusion's gate (2) holds a one-day spike back
    # a warm desert that is always +2.4 dB is its own baseline: silent
    assert det.detect([_ev(27, 2.4, **{**base, "base_db": 2.3})], {}, NOW, aoi=Aoi()) == []


def test_solar_radio_event_lowers_confidence() -> None:
    sw = GeoEvent(
        id="sw",
        ts=datetime(2026, 9, 27, 9, tzinfo=UTC),
        source="swpc",
        domain="space",
        kind="space_weather",
        attrs={"r_scale": 3},
    )
    (plain,) = det.detect([_ev(27, 3.7)], {}, NOW, aoi=Aoi())
    (expl,) = det.detect([_ev(27, 3.7)], {}, NOW, aoi=Aoi(), space_weather=[sw])
    assert expl.coverage.confidence == pytest.approx(plain.coverage.confidence * 0.5, abs=1e-3)
    assert expl.gaps[0].startswith("strong solar radio event (R3)")


# ---------------------------------------------------------------- fusion


def _adsb_jam() -> Evidence:
    return Evidence(
        id="adsb-jam",
        kind=AlertKind.GNSS_JAMMING,
        detector="jamming",
        detector_version="x",
        window_start=NOW - timedelta(minutes=30),
        window_end=NOW,
        aoi="gulf",
        h3_cell=h3.latlng_to_cell(27.3, 51.8, 4),
        h3_res=4,
        lat=27.3,
        lon=51.8,
        score=0.8,
        counterfactual="x",
        coverage=Coverage(confidence=0.8, providers=["adsb_lol"]),
        records=[RecordRef(source="adsb_lol", id="4b1805@x")],
        persistence=3,
        created_at=NOW,
        bands=[Band.L1_GNSS],
    )


def test_cygnss_corroborates_adsb_jamming_as_independent_family() -> None:
    (cy,) = det.detect([_ev(26, 3.6), _ev(27, 3.7)], {}, NOW, aoi=Aoi())
    xs = cross_source([cy, _adsb_jam()], [], [])
    assert [x.evidence_id for x in xs[cy.id]] == ["adsb-jam"]
    assert [x.evidence_id for x in xs["adsb-jam"]] == [cy.id]
    alerts = {a.h3_cell: a for a in fuse([cy, _adsb_jam()], [], NOW, aoi="gulf")}
    a = alerts[cy.h3_cell]
    assert a.corroboration_status == "corroborated"
    assert a.severity in (Severity.LOW, Severity.MEDIUM)  # single band, capped confidence
    assert "seen from orbit (CYGNSS)" in a.summary and "+3.7 dB" in a.summary
