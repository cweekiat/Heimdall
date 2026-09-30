"""P3 pipeline wiring (task 022a): thermal/outage/night-lights per tick, overpasses, key gating."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from heimdall.adapters.secrets_file import FileSecretStore
from heimdall.config import AOI, Settings, settings, sources
from heimdall.detect import DETECTORS
from heimdall.geo import h3_cell
from heimdall.ingest.celestrak import parse_gp
from heimdall.ingest.registry import build_connectors
from heimdall.pipeline import tick, update_overpasses
from heimdall.schemas import KEY_CONNECTORS, AlertKind
from tests.synthetic import outage as syn_out
from tests.synthetic import thermal as syn_th
from tests.unit.fakes import FakeStore
from tests.unit.test_fusion import GRADES, PB

NOW = syn_th.NOW
SCS = AOI(name="south_china_sea", role="primary", lat=12.5, lon=113.5, radius_nm=250)
MM = AOI(name="myanmar", role="primary", lat=20.5, lon=96.5, radius_nm=250)


def _cfg(*aois: AOI) -> Settings:
    return Settings(aois=list(aois), detectors=settings().detectors)


def _tick(store: FakeStore, cfg: Settings, now: datetime):  # type: ignore[no-untyped-def]
    return tick(store, cfg, now, playbook=PB, source_grades=GRADES, sources_cfg={})


def test_detectors_registered() -> None:
    assert {"thermal", "outage", "night_lights"} <= set(DETECTORS)
    assert DETECTORS["thermal"].kind == AlertKind.THERMAL
    assert DETECTORS["outage"].kind == DETECTORS["night_lights"].kind == AlertKind.OUTAGE


def test_thermal_cluster_needs_two_windows() -> None:
    ev, base = syn_th.new_cluster()
    store, cfg = FakeStore(), _cfg(SCS)
    store.insert_events([*ev, *base])
    r1 = _tick(store, cfg, NOW)
    th1 = [e for e in r1.evidence if e.kind == AlertKind.THERMAL]
    assert len(th1) == 1 and th1[0].persistence == 1
    assert r1.alerts == []
    r1b = _tick(store, cfg, NOW + timedelta(minutes=5))  # same window: no extra persistence
    assert [e.persistence for e in r1b.evidence if e.kind == AlertKind.THERMAL] == [1]
    r2 = _tick(store, cfg, NOW + timedelta(hours=1))
    assert [e.persistence for e in r2.evidence if e.kind == AlertKind.THERMAL] == [2]
    alerts = [a for a in r2.alerts if a.kind == AlertKind.THERMAL]
    assert len(alerts) == 1
    cell4 = h3_cell(SCS.lat, SCS.lon, 4)
    assert store.metric_history(cell4, "firms_detections", NOW - timedelta(days=1))


def test_ioda_drop_alerts_and_cloudflare_alone_does_not() -> None:
    cfg = _cfg(MM)
    store = FakeStore()
    store.insert_events(syn_out.ioda_drop())
    r1 = _tick(store, cfg, NOW)
    assert [e.persistence for e in r1.evidence if e.kind == AlertKind.OUTAGE] == [1]
    r2 = _tick(store, cfg, NOW + timedelta(minutes=5))
    assert [e.persistence for e in r2.evidence if e.kind == AlertKind.OUTAGE] == [2]
    assert [a.kind for a in r2.alerts] == [AlertKind.OUTAGE]

    cf = FakeStore()
    cf.insert_events([syn_out.annotation()])
    for m in (0, 5):
        r = _tick(cf, cfg, NOW + timedelta(minutes=m))
        assert not [e for e in r.evidence if e.kind == AlertKind.OUTAGE] and r.alerts == []


def test_night_lights_drop_becomes_outage_evidence_and_metric() -> None:
    store, cfg = FakeStore(), _cfg(MM)
    store.insert_events(syn_out.city_dark(nights=3))
    r = _tick(store, cfg, NOW)
    (e,) = [x for x in r.evidence if x.kind == AlertKind.OUTAGE]
    assert e.detector == "night_lights" and e.persistence == 3
    got = store.metric_history(e.h3_cell or "", "night_lights_mean", NOW - timedelta(days=60))
    assert got


def test_overpasses_upserted_without_duplicates() -> None:
    now = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    fix = Path(__file__).parent.parent / "fixtures" / "celestrak" / "gp_sentinel2a.json"
    omm, _ = parse_gp(json.loads(fix.read_text()))
    store = FakeStore()
    store.insert_events(omm)
    ccfg = {
        "satellites": {40697: {"name": "SENTINEL-2A", "sensor": "MSI"}},
        "pass_horizon_h": 48,
        "min_elev_deg": 20,
    }
    cfg = _cfg(MM)
    tick(store, cfg, now, playbook=PB, source_grades=GRADES, sources_cfg={"celestrak": ccfg})
    first = [e for e in store.events if e.kind == "overpass"]
    assert first
    tick(
        store,
        cfg,
        now + timedelta(minutes=5),
        playbook=PB,
        source_grades=GRADES,
        sources_cfg={"celestrak": ccfg},
    )
    assert [e.id for e in store.events if e.kind == "overpass"] == [e.id for e in first]
    # forced recompute a day later (refresh gate passed): overlapping passes keep their ids
    update_overpasses(store, cfg.aois, ccfg, now + timedelta(days=1))
    passes = sorted(e.ts for e in store.events if e.kind == "overpass")
    assert len(passes) == len({e.id for e in store.events if e.kind == "overpass"})
    assert all(b - a > timedelta(minutes=10) for a, b in pairwise(passes))


def test_keys_listed_unset_and_sources_need_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("FIRMS_MAP_KEY", "CLOUDFLARE_RADAR_TOKEN", "EARTHDATA_TOKEN"):
        assert name in KEY_CONNECTORS
        monkeypatch.delenv(name, raising=False)
    status = {k.name: k for k in FileSecretStore(tmp_path / "keys.json").status()}
    for name in ("FIRMS_MAP_KEY", "CLOUDFLARE_RADAR_TOKEN", "EARTHDATA_TOKEN"):
        assert not status[name].is_set
    keyed = {"firms", "cloudflare_radar", "black_marble"}
    conns = build_connectors(settings(), sources(), enabled=keyed)
    assert {c.name for c in conns} == keyed
    assert {c.name for c in conns if c.health().status == "needs_key"} == keyed
    store = FakeStore()
    assert _tick(store, _cfg(SCS, MM), NOW).alerts == []  # tick completes with no P3 data


def test_p3_connectors_have_impl() -> None:
    cfg = sources()["sources"]
    for k in ("firms", "open_meteo", "swpc", "ioda", "cloudflare_radar", "black_marble", "stac"):
        assert cfg[k]["impl"] == f"heimdall.ingest.{k}:build"
    conns = build_connectors(settings(), sources(), enabled={"open_meteo", "swpc", "ioda"})
    assert {c.name: c.health().status for c in conns} == {
        "open_meteo": "ok",
        "swpc": "ok",
        "ioda": "ok",
    }
