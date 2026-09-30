from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from heimdall.fuse.confounders import assess
from heimdall.schemas import AlertKind, GeoEvent
from tests.synthetic import context as syn

CFG: dict[str, Any] = {}
DAY = syn.T0  # 12:00Z: 5N 105E is local ~19 h... use EVENING for scintillation tests


def test_kp6_lowers_to_08_with_note() -> None:
    f, notes, gaps = assess(syn.evidence(lat=55.0, lon=20.0), [syn.space_weather(6.0, g=2)], CFG)
    assert f == 0.8 and gaps == []
    assert [n.note for n in notes if n.note.startswith("Kp")] == ["Kp 6.0 (G2) during window"]
    assert all(n.relation == "context" for n in notes)


def test_kp8_severe_and_kp_below_threshold() -> None:
    ev = syn.evidence(lat=55.0, lon=20.0)
    assert assess(ev, [syn.space_weather(8.0)], CFG)[0] == 0.5
    assert assess(ev, [syn.space_weather(4.9)], CFG)[:2] == (1.0, [])
    assert assess(ev, [syn.space_weather(5.0)], CFG)[0] == 0.8


def test_no_swpc_data_gives_factor_one_and_gap() -> None:
    f, notes, gaps = assess(syn.evidence(lat=55.0, lon=20.0), [], CFG)
    assert (f, notes) == (1.0, []) and gaps == ["space weather context unavailable (lag)"]
    old = syn.space_weather(8.0, ts=syn.T0 - timedelta(hours=20))
    assert assess(syn.evidence(lat=55.0, lon=20.0), [old], CFG)[0] == 1.0


def test_equatorial_evening_scintillation_vs_baltic() -> None:
    sw = [syn.space_weather(2.0, ts=syn.EVENING)]
    tropics = syn.evidence(lat=5.0, lon=105.0, start=syn.EVENING)
    f, notes, _ = assess(tropics, sw, CFG)
    assert f == 0.85 and any("scintillation" in n.note for n in notes)
    baltic = syn.evidence(lat=55.0, lon=20.5, start=syn.EVENING)
    assert assess(baltic, sw, CFG)[0] == 1.0
    noon = syn.evidence(lat=5.0, lon=105.0, start=syn.T0 - timedelta(hours=3))
    assert assess(noon, [syn.space_weather(2.0, ts=noon.window_start)], CFG)[0] == 1.0


def test_factors_multiply_and_floor() -> None:
    ev = syn.evidence(lat=5.0, lon=105.0, start=syn.EVENING)
    sw = [syn.space_weather(6.0, ts=syn.EVENING)]
    assert abs(assess(ev, sw, CFG)[0] - 0.68) < 1e-9
    assert assess(ev, sw, {"min_factor": 0.75})[0] == 0.75


def test_outage_storm_and_cloud_rules() -> None:
    ev = syn.evidence(AlertKind.OUTAGE, detector="outage")
    f, notes, _ = assess(ev, [syn.weather(code=95)], CFG)
    assert f == 0.7 and "weather code 95" in notes[0].note
    assert assess(ev, [syn.weather(lat=8.0, code=95)], CFG)[2] == [
        "weather context unavailable (lag)"
    ]
    assert assess(ev, [syn.weather(cloud=95.0)], CFG)[0] == 1.0  # cloud only matters for lights
    nl = syn.evidence(AlertKind.OUTAGE, detector="night_lights")
    f2, notes2, _ = assess(nl, [syn.weather(cloud=90.0), syn.weather(cloud=70.0)], CFG)
    assert f2 == 0.5 and "cloud cover 80 %" in notes2[0].note


def test_ais_gap_wind_and_other_kinds() -> None:
    ev = syn.evidence(AlertKind.AIS_GAP, detector="ais_gap")
    assert assess(ev, [syn.weather(wind=25.0)], CFG)[0] == 0.9
    assert assess(ev, [syn.weather(wind=10.0)], CFG)[0] == 1.0
    assert assess(syn.evidence(AlertKind.THERMAL), [syn.weather(wind=99.0)], CFG) == (1.0, [], [])


def test_injection_never_in_notes() -> None:
    ctx = [syn.space_weather(6.0), syn.weather(code=95, cloud=95.0, wind=30.0)]
    for kind, det in [(AlertKind.GNSS_JAMMING, "jamming"), (AlertKind.OUTAGE, "night_lights")]:
        _, notes, gaps = assess(syn.evidence(kind, detector=det), ctx, CFG)
        assert notes and "IGNORE" not in " ".join(n.note for n in notes) + " ".join(gaps)


def test_factor_always_in_bounds_property() -> None:
    rng = random.Random(7)
    kinds = [AlertKind.GNSS_JAMMING, AlertKind.OUTAGE, AlertKind.AIS_GAP, AlertKind.THERMAL]
    weird: list[Any] = [None, "x", float("nan"), float("inf"), -5, 0, 5, 7.5, 99, True, 1e9]
    for _ in range(400):
        ev = syn.evidence(
            rng.choice(kinds),
            lat=rng.uniform(-90, 90),
            lon=rng.uniform(-180, 180),
            start=syn.T0 + timedelta(hours=rng.uniform(-30, 30)),
            detector=rng.choice(["night_lights", "outage", "jamming"]),
        )
        ctx: list[GeoEvent] = []
        for _ in range(rng.randint(0, 6)):
            ts = ev.window_start + timedelta(hours=rng.uniform(-10, 3))
            w = syn.weather(ev.lat or 0.0, ev.lon or 0.0, ts)
            w.attrs.update(
                cloud_cover=rng.choice(weird),
                wind_speed_ms=rng.choice(weird),
                weather_code=rng.choice(weird),
            )
            s = syn.space_weather(0.0, ts)
            s.attrs["kp"] = rng.choice(weird)
            ctx += [w, s]
        cfg = {"min_factor": rng.choice([0.0, 0.3, 0.9]), "storm_factor": rng.choice([0.7, 5, -2])}
        f, _, _ = assess(ev, ctx, cfg)
        assert cfg["min_factor"] <= f <= 1.0
