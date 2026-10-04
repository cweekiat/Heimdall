"""060: HF reception at the receiver cell - ingest grouping, relative-to-neighbours detector."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from heimdall.adapters.memory_store import MemoryStore
from heimdall.detect import hf_rx
from heimdall.ingest.pskreporter import Aggregator, parse_reports
from heimdall.ingest.wspr import build_query, parse_wspr
from heimdall.pipeline import HfCache
from heimdall.schemas import Band, Domain, GeoEvent

FIX = Path(__file__).parents[1] / "fixtures"
T_END = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # night in Japan: no flare question
TARGET = (36.0, 140.0)
KEY = ("wspr_live", 7, *TARGET)
NEIGHBOURS = [(36.0, 138.0), (34.0, 136.0), (38.0, 142.0)]
SWING = [0.0, 2.5, -2.0, 1.0, -3.0, 2.0, -1.0, 3.0, -2.5, 0.5, 1.5, -1.5, 2.0, -0.5, 1.0]


@dataclass
class Area:
    name: str = "Tokyo"
    lat: float = 35.7
    lon: float = 139.7
    radius_nm: float = 100.0


def rx_bin(t: datetime, lat: float, lon: float, *, n: float, snr: float, rx: int = 4) -> GeoEvent:
    return GeoEvent(
        id=f"wsprrx|{t:%Y%m%dT%H%M}|7|{lat:g}|{lon:g}",
        ts=t,
        source="wspr_live",
        domain=Domain.SPECTRUM,
        kind="hf_rx",
        lat=lat,
        lon=lon,
        value=n,
        attrs={"band_mhz": 7, "n": n, "n_rx": rx, "snr_med": snr, "geo": "receiver_cell"},
    )


def history(
    *,
    target_db: float = 0.0,
    region_db: float = 0.0,
    target_keep: float = 1.0,
    neighbours: bool = True,
) -> list[GeoEvent]:
    """Each day shares a regional swing (SWING, dB) across all cells, like real propagation;
    day 0 (today) adds `region_db` to every cell and `target_db` to the target only."""
    out = []
    span = hf_rx.span()
    cells = [TARGET, *(NEIGHBOURS if neighbours else [])]
    for d in range(15):
        swing = SWING[d] + (region_db if d == 0 else 0.0)
        t = T_END - timedelta(days=d) - span
        while t < T_END - timedelta(days=d):
            for c in cells:
                own = target_db if d == 0 and c == TARGET else 0.0
                keep = target_keep if d == 0 and c == TARGET else 1.0
                base = -18.0 if c == TARGET else -15.0
                out.append(rx_bin(t, *c, n=20.0 * keep, snr=base + swing + own))
            t += timedelta(minutes=10)
    return out


def state(evs: list[GeoEvent], ctx: list[GeoEvent] | None = None) -> hf_rx.RxState:
    return hf_rx.prepare(hf_rx.bins_from_events(evs), ctx or [], T_END, {})


def test_wspr_receiver_query_and_parse() -> None:
    q = build_query(T_END - timedelta(hours=1), T_END, grid_deg=2.0, receiver=True)
    assert "round(rx_lat / 2.0)" in q and "rx_lat BETWEEN" in q and "mlat" not in q
    payload = {
        "data": [
            {"t": "2026-09-30 11:00:00", "band": 7, "glat": 36, "glon": 140, "n": 31, "n_rx": 4}
        ]
    }
    (e,), dropped = parse_wspr(payload, grid_deg=2.0, receiver=True)
    assert dropped == 0 and e.kind == "hf_rx" and e.id.startswith("wsprrx|")
    assert e.attrs["geo"] == "receiver_cell" and e.attrs["grid_deg"] == 2.0


def test_psk_receiver_cells_alongside_midpoints() -> None:
    reports, _, _ = parse_reports((FIX / "pskreporter/query_ft8.xml").read_bytes())
    agg = Aggregator(bbox=(-180, -90, 180, 90), rx_grid_deg=2.0)
    touched, _ = agg.add(reports)
    evs = [agg.event(k) for k in touched]
    mids = [e for e in evs if e.kind == "hf_spots"]
    rxs = [e for e in evs if e.kind == "hf_rx"]
    assert mids and rxs and all(e.attrs["geo"] == "receiver_cell" for e in rxs)
    assert sum(e.attrs["n"] for e in rxs) == sum(e.attrs["n"] for e in mids)  # same reports
    assert all(e.lat is not None and e.lat % 2 == 0 for e in rxs)
    assert not json.dumps([e.attrs for e in rxs]).count("rx_key")  # callsigns never stored
    assert Aggregator(bbox=(-180, -90, 180, 90)).add(reports)[0] == {
        e.id for e in mids
    }  # off by default


def test_local_drop_flags_with_persistence_and_evidence() -> None:
    st = state(history(target_db=-9.0))
    f = st.flags[0][KEY]
    assert f.flagged and f.snr_drop and f.local is True and f.rel is not None
    assert f.rel.d_snr is not None and f.rel.d_snr <= -8.5
    (ev,) = hf_rx.detect(st, Area(), {}, T_END)
    assert ev.detector == "hf_rx" and ev.bands == [Band.HF] and ev.persistence == 3
    passed = {r.rule for r in ev.rules if r.passed}
    assert {"hf_rx_snr_drop", "hf_rx_local"} <= passed
    assert ev.coverage.confidence <= 0.5 and "never names an emitter" in ev.gaps[-1]
    # far from the area: not this area's business
    assert hf_rx.detect(st, Area(lat=1.3, lon=103.8), {}, T_END) == []


def test_regional_drop_is_propagation_not_local() -> None:
    st = state(history(region_db=-9.0))
    assert not any(f.flagged for w in st.flags for f in w.values())


def test_neighbours_rising_do_not_make_the_cell_look_down() -> None:
    evs = history(region_db=9.0, target_db=-9.0)  # target unchanged, neighbours +9 dB
    st = state(evs)
    assert not any(f.flagged for w in st.flags for f in w.values())


def test_decode_drop_per_receiver() -> None:
    st = state(history(target_keep=0.25))
    f = st.flags[0][KEY]
    assert f.rate_drop and f.flagged


def test_no_neighbours_uses_stricter_absolute_test() -> None:
    # alone, the shared day-to-day swing is noise: the 9 dB drop the relative test catches is
    # within the lone cell's spread; a 14 dB drop is caught at lower confidence
    assert not state(history(target_db=-9.0, neighbours=False)).flags[0]
    st = state(history(target_db=-14.0, neighbours=False))
    f = st.flags[0].get(KEY)
    assert f is not None and f.rel is None and f.local is None
    (ev,) = hf_rx.detect(st, Area(), {}, T_END)
    local = next(r for r in ev.rules if r.rule == "hf_rx_local")
    assert not local.passed and "unknown" in local.description
    assert ev.coverage.confidence < 0.5 * 0.7


def test_storm_explains_away() -> None:
    kp = GeoEvent(
        id="swpc|kp",
        ts=T_END - timedelta(hours=2),
        source="swpc",
        domain=Domain.SPACE,
        kind="space_weather",
        attrs={"kp": 6.0},
    )
    st = state(history(target_db=-9.0), [kp])
    f = st.flags[0][KEY]
    assert not f.flagged and "geomagnetic storm" in f.explained[0]


def test_pipeline_cache_serves_receiver_detector() -> None:
    store = MemoryStore()
    store.insert_events(history(target_db=-9.0))
    st = HfCache(hf_rx).state_for(store, {}, T_END + timedelta(minutes=20))
    assert isinstance(st, hf_rx.RxState)
    assert any(f.flagged for f in st.flags[0].values())
