"""033/034: per-area band indicators, states against own history, experimental Evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.config import AOI, Settings, settings
from heimdall.detect import spectrum_state as ss
from heimdall.pipeline import tick
from heimdall.schemas import AircraftObs, AlertKind, Band, Domain, GeoEvent, VesselObs
from tests.unit.fakes import FakeStore
from tests.unit.test_fusion import GRADES, PB

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
MM = AOI(name="myanmar", role="primary", lat=20.5, lon=96.5, radius_nm=250)


def ev(kind: str, i: int, age_min: float = 10, lat: float = 20.5, **attrs: object) -> GeoEvent:
    return GeoEvent(id=f"{kind}|{i}", ts=NOW - timedelta(minutes=age_min), source={
        "hf_spots": "wspr_live", "sar_rfi": "sentinel1_rfi", "l_band_rfi": "smap_rfi",
        "sat_observation": "satnogs", "space_weather": "swpc"}[kind],
        domain=Domain.SPECTRUM, kind=kind, lat=lat, lon=96.5, attrs=dict(attrs))  # fmt: skip


def test_indicators_per_band() -> None:
    t = NOW - timedelta(minutes=5)
    air = [
        AircraftObs(ts=t, hex=f"{i:06x}", provider="adsb.fi", lat=20.5, lon=96.5,
                    src_type="adsb_icao", nic=3 if i < 4 else 9)
        for i in range(10)
    ]  # fmt: skip
    ships = [
        VesselObs(ts=t, mmsi=100000000 + i, provider="aisstream", lat=20.5, lon=96.5)
        for i in range(3)
    ]
    events = [
        ev("hf_spots", 1, n=12, snr_med=-10), ev("hf_spots", 2, age_min=90, n=99),  # 2nd too old
        ev("sar_rfi", 1, rfi_detected=True), ev("sar_rfi", 2, rfi_detected=False),
        ev("l_band_rfi", 1, n_footprints=20, n_rfi_h=5, n_rfi_v=1),
        ev("sat_observation", 1, status="bad"), ev("sat_observation", 2, status="good"),
        ev("hf_spots", 3, lat=60.0, n=50),  # far away
    ]  # fmt: skip
    ind = ss.compute_indicators(MM, air, ships, events, NOW)
    assert ind[Band.L1_GNSS].value == 40.0 and ind[Band.L1_GNSS].n == 10
    assert ind[Band.L_1090].value == 10 and ind[Band.VHF_MARINE].value == 3
    assert ind[Band.HF].value == 12 and ind[Band.HF].sources == ["wspr_live"]
    assert ind[Band.C_SAR].value == 50.0 and ind[Band.L_1400].value == 25.0
    assert ind[Band.SAT_DOWNLINK].value == 50.0


def test_states_and_direction() -> None:
    hist = [10.0, 11.0, 9.0, 10.0] * 80  # 320 samples, median 10
    mk = lambda b, v, n=50: ss.Indicator(b, v, n)  # noqa: E731
    assert ss.assess("a", mk(Band.C_SAR, None, 0), hist).state == "no_coverage"
    assert ss.assess("a", mk(Band.C_SAR, 50.0), hist[:10]).state == "learning"
    assert ss.assess("a", mk(Band.VHF_MARINE, 10.0), hist).state == "normal"
    low = ss.assess("a", mk(Band.VHF_MARINE, 1.0), hist)  # fewer ships than usual = trouble
    assert low.state == "anomalous" and low.z is not None and low.z > 0
    high = ss.assess("a", mk(Band.VHF_MARINE, 30.0), hist)  # more ships is not trouble
    assert high.state == "normal"


def test_hf_uses_same_hour_history_and_space_weather_explains() -> None:
    hist = [(NOW - timedelta(days=d, hours=h), float(h)) for d in range(1, 8) for h in range(24)]
    same = ss.history_for(Band.HF, hist, NOW)
    assert set(same) <= {0.0, 1.0, 23.0}
    sw = [ev("space_weather", 1, kp=6.0, r_scale=2)]
    ex = ss.natural_explanations(Band.HF, sw, NOW)
    assert any("Kp 6" in x for x in ex) and any("R2" in x for x in ex)
    assert ss.natural_explanations(Band.C_SAR, sw, NOW) == []


def test_evidence_only_for_experimental_bands_capped() -> None:
    hist = [0.0, 5.0] * 200
    st = ss.assess("myanmar", ss.Indicator(Band.C_SAR, 100.0, 8, sources=["sentinel1_rfi"]), hist)
    win = (NOW - timedelta(hours=1), NOW)
    e = ss.evidence(st, MM, NOW, win)
    assert e is not None and e.kind == AlertKind.SPECTRUM_ANOMALY and e.bands == [Band.C_SAR]
    assert e.coverage.confidence <= 0.6 and e.records[0].source == "sentinel1_rfi"
    assert "never names an emitter" in " ".join(e.gaps) and "Would not fire" in (
        e.counterfactual or ""
    )
    gnss = ss.assess("myanmar", ss.Indicator(Band.L1_GNSS, 90.0, 50), hist)
    assert gnss.state == "anomalous" and ss.evidence(gnss, MM, NOW, win) is None


def test_pipeline_records_history_and_alerts_after_persistence() -> None:
    store = FakeStore()
    cfg = Settings(aois=[MM], detectors=settings().detectors)
    key = f"{MM.name}|{Band.C_SAR.value}"
    for i in range(400):  # two quiet weeks of C-band history
        store.record_metric(
            key, ss.METRIC, NOW - timedelta(days=10) + timedelta(minutes=5 * i), 0.0
        )
    store.insert_events([ev("sar_rfi", i, age_min=60, rfi_detected=True) for i in range(3)])
    alerts = []
    for k in range(3):
        r = tick(store, cfg, NOW + timedelta(minutes=5 * k), playbook=PB, source_grades=GRADES,
                 sources_cfg={})  # fmt: skip
        alerts = [a for a in r.alerts if a.kind == AlertKind.SPECTRUM_ANOMALY]
    assert (
        alerts and Band.C_SAR in alerts[0].bands and alerts[0].severity.value in ("low", "medium")
    )
    assert store.metric_history(key, ss.N_METRIC, NOW - timedelta(hours=1))


def test_status_api() -> None:
    st = MemoryStore()
    now = datetime.now(UTC)
    aoi = settings().aois[0]
    for i in range(300):
        st.record_metric(
            f"{aoi.name}|l1_gnss", ss.METRIC, now - timedelta(minutes=5 * (300 - i)), 2.0
        )
    st.record_metric(f"{aoi.name}|l1_gnss", ss.METRIC, now, 40.0)
    st.record_metric(f"{aoi.name}|l1_gnss", ss.N_METRIC, now, 30.0)
    c = TestClient(create_app(st), base_url="http://127.0.0.1:8000")
    body = c.get("/api/spectrum/status").json()
    row = next(a for a in body["areas"] if a["aoi"] == aoi.name)
    cell = row["bands"]["l1_gnss"]
    assert cell["state"] == "anomalous" and cell["value"] == 40.0 and len(cell["spark"]) == 24
    assert row["bands"]["hf"]["state"] == "no_coverage"
    assert body["bands"][0]["band"] == "l1_gnss"
