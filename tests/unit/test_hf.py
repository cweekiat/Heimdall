"""P6 (045): HF path-midpoint anomaly detector, X-ray flare confounder, pipeline cache."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from heimdall.adapters.memory_store import MemoryStore
from heimdall.detect import hf
from heimdall.ingest.swpc import flare_class, parse_xray
from heimdall.pipeline import HfCache
from heimdall.schemas import Band, Domain, GeoEvent

FIX = Path(__file__).parents[1] / "fixtures" / "swpc"
# 04:00 UTC = noon in Singapore: midpoint (2, 104) sunlit; (2, -76) is at night
T_END = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
TARGET = (2.0, 104.0)
OTHER = (10.0, 110.0)


@dataclass
class Area:
    name: str = "Singapore"
    lat: float = 1.3
    lon: float = 103.8
    radius_nm: float = 100.0


def spot(
    t: datetime,
    lat: float,
    lon: float,
    n: float,
    *,
    rx: int = 6,
    snr: float = -10.0,
    band: int = 14,
    source: str = "wspr_live",
) -> GeoEvent:
    return GeoEvent(
        id=f"{source}|{t:%Y%m%dT%H%M}|{band}|{lat:g}|{lon:g}",
        ts=t,
        source=source,
        domain=Domain.SPECTRUM,
        kind="hf_spots",
        lat=lat,
        lon=lon,
        value=n,
        attrs={"band_mhz": band, "n": n, "n_rx": rx, "snr_med": snr},
    )


def history(
    *,
    now_target: float = 2.0,
    now_other: float = 200.0,
    days: int = 14,
    rx: int = 6,
    now_snr: float = -10.0,
    target: tuple[float, float] = TARGET,
) -> list[GeoEvent]:
    """10-min bins over the span: target cell usually 20/bin (120/h), other cell 200/bin."""
    out = []
    span = hf.span()
    for d in range(days + 1):
        t = T_END - timedelta(days=d) - span
        while t < T_END - timedelta(days=d):
            today = d == 0
            out.append(
                spot(
                    t,
                    *target,
                    now_target if today else 20.0,
                    rx=rx,
                    snr=now_snr if today else -10.0,
                )
            )
            out.append(spot(t, *OTHER, now_other if today else 200.0))
            t += timedelta(minutes=10)
    return out


def run(events: list[GeoEvent], context: list[GeoEvent] = ()) -> hf.HfState:  # type: ignore[assignment]
    return hf.prepare(hf.bins_from_events(events), list(context), T_END, created=T_END)


def test_local_count_drop_fires_with_persistence_and_explains_itself() -> None:
    st = run(history())
    ev = hf.detect(st, Area())
    assert len(ev) == 1
    e = ev[0]
    assert e.bands == [Band.HF] and e.persistence == 3 and e.detector == "hf_path"
    assert e.rules[0].rule == "hf_count_drop" and e.rules[0].passed
    assert e.features["usual_n"] == 120.0 and e.features["n"] == 12.0
    assert e.coverage.confidence <= 0.5
    assert any("switched off" in g for g in e.gaps)
    # far from the area: nothing
    assert hf.detect(st, Area(lat=40.0, lon=140.0)) == []


def test_normal_hour_is_silent() -> None:
    assert hf.detect(run(history(now_target=20.0)), Area()) == []


def test_flare_on_sunlit_midpoint_explains_the_drop() -> None:
    xr = parse_xray(
        [
            {
                "time_tag": (T_END - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "energy": "0.1-0.8nm",
                "flux": 2.7e-4,
            }
            for m in range(0, 160, 5)
        ]
    )[0]
    st = run(history(), xr)
    assert hf.detect(st, Area()) == []
    assert any("X2.7" in x for f in st.explained() for x in f.explained)
    # same flare, midpoint on the night side: not explained
    night = run(history(target=(2.0, -76.0)), xr)
    assert hf.detect(night, Area(lat=2.0, lon=-76.0))


def test_storm_and_region_wide_drop_explain() -> None:
    kp = [
        GeoEvent(
            id="kp",
            ts=T_END - timedelta(hours=2),
            source="swpc",
            domain=Domain.SPACE,
            kind="space_weather",
            attrs={"kp": 6.0, "r_scale": None},
        )
    ]
    assert hf.detect(run(history(), kp), Area()) == []
    st = run(history(now_other=20.0))  # whole box down to 10 %: feed or region-wide
    assert hf.detect(st, Area()) == []
    assert any("whole region" in x for f in st.explained() for x in f.explained)


def test_few_receivers_or_short_baseline_never_fire() -> None:
    assert hf.detect(run(history(rx=1)), Area()) == []
    assert hf.detect(run(history(days=3)), Area()) == []


def test_snr_drop_fires_without_count_drop() -> None:
    ev = hf.detect(run(history(now_target=20.0, now_snr=-22.0)), Area())
    assert len(ev) == 1 and ev[0].rules[0].rule == "hf_snr_drop"
    assert ev[0].features["usual_snr_db"] == -10.0


def test_sun_elevation_day_and_night() -> None:
    assert hf.sun_elevation_deg(1.3, 103.8, datetime(2026, 9, 30, 5, 0, tzinfo=UTC)) > 70
    assert hf.sun_elevation_deg(1.3, 103.8, datetime(2026, 9, 30, 17, 0, tzinfo=UTC)) < -60


def test_xray_fixture_and_flare_class() -> None:
    evs, dropped = parse_xray(json.loads((FIX / "xrays-6-hour.json").read_text()))
    assert evs and dropped == 0
    assert all(e.kind == "solar_xray" and e.ts.minute % 5 == 0 for e in evs)
    assert len({e.id for e in evs}) == len(evs)
    assert flare_class(2.7e-4) == "X2.7" and flare_class(3e-6) == "C3.0"
    assert flare_class(1.2e-5) == "M1.2"


def test_pipeline_cache_reads_history_once_per_bin() -> None:
    store = MemoryStore()
    store.insert_events(history())
    cache = HfCache()
    st = cache.state_for(store, {}, T_END + timedelta(minutes=20))
    assert st is not None and hf.detect(st, Area())
    n_hist = len(cache.hist)
    assert cache.state_for(store, {}, T_END + timedelta(minutes=25)) is st  # same window
    st2 = cache.state_for(store, {}, T_END + timedelta(minutes=30))  # next 10-min window
    assert st2 is not st and len(cache.hist) == n_hist  # one new bin per day, one evicted
    assert cache.state_for(store, {"enabled": False}, T_END) is None


def test_spectrum_board_no_longer_raises_hf_evidence() -> None:
    """P6: HF alerts come from the path detector; the board keeps the HF state only."""
    from heimdall.detect import spectrum_state as ss

    st = ss.BandState("Singapore", Band.HF, "anomalous", 5.0, 50, median=100.0, z=4.0, sigma=10.0)
    assert ss.evidence(st, Area(), T_END, (T_END - timedelta(hours=1), T_END)) is None
