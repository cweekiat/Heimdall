"""AISStream parser: message types, not-available values, MMSI validation, static merge."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from heimdall.ingest.ais import StaticCache, parse_aisstream, parse_time_utc
from heimdall.schemas import VesselObs

FIX = Path(__file__).parent.parent / "fixtures" / "aisstream" / "stream_sample.jsonl"
RECV = datetime(2026, 9, 30, 7, 0, tzinfo=UTC)


def _msgs() -> list[dict[str, Any]]:
    return [json.loads(x) for x in FIX.read_text().splitlines() if x.strip()]


def _pos(mtype: str, body: dict[str, Any], mmsi: int = 563000001, t: Any = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"MMSI": mmsi, "ShipName": "  ", "latitude": 3.0, "longitude": 100.8}
    if t is not None:
        meta["time_utc"] = t
    return {"MessageType": mtype, "MetaData": meta, "Message": {mtype: {"UserID": mmsi, **body}}}


def _parse_all() -> tuple[list[VesselObs], StaticCache]:
    cache = StaticCache()
    out = [o for m in _msgs() if (o := parse_aisstream(m, cache, RECV)) is not None]
    return out, cache


def test_fixture_yields_expected_positions() -> None:
    obs, _ = _parse_all()
    assert len(obs) == 16
    assert {o.mmsi for o in obs} >= {563000001, 538000002, 548000020, 477000003}
    assert all(o.provider == "aisstream" and o.h3_r4 and o.h3_r6 for o in obs)


def test_position_report_fields() -> None:
    o = parse_aisstream(
        _pos(
            "PositionReport",
            {
                "Latitude": 3.06,
                "Longitude": 100.91,
                "Sog": 12.3,
                "Cog": 87.0,
                "TrueHeading": 85,
                "NavigationalStatus": 5,
            },
        ),
        StaticCache(),
        RECV,
    )
    assert o is not None
    assert (o.lat, o.lon, o.sog, o.cog, o.heading, o.nav_status) == (
        3.06,
        100.91,
        12.3,
        87.0,
        85,
        5,
    )


def test_class_b_and_extended_class_b() -> None:
    c = StaticCache()
    b = parse_aisstream(
        _pos("StandardClassBPositionReport", {"Latitude": 2.9, "Longitude": 101.0, "Sog": 4.2}),
        c,
        RECV,
    )
    assert b is not None and b.sog == 4.2 and b.nav_status is None
    x = parse_aisstream(
        _pos(
            "ExtendedClassBPositionReport",
            {"Latitude": 13.0, "Longitude": 113.0, "Name": "BOAT TWO@@", "Type": 37},
            548000020,
        ),
        c,
        RECV,
    )
    assert x is not None and x.name == "BOAT TWO" and x.ship_type == 37


def test_not_available_values_become_none() -> None:
    o = parse_aisstream(
        _pos(
            "PositionReport",
            {
                "Latitude": 12.7,
                "Longitude": 113.7,
                "Sog": 102.3,
                "Cog": 360.0,
                "TrueHeading": 511,
                "NavigationalStatus": 15,
            },
        ),
        StaticCache(),
        RECV,
    )
    assert o is not None
    assert o.sog is None and o.cog is None and o.heading is None and o.nav_status is None


def test_implausible_positions_dropped() -> None:
    c = StaticCache()
    for lat, lon in [(91.0, 113.0), (12.0, 181.0), (0.0, 0.0), (-95.0, 10.0)]:
        m = _pos("PositionReport", {"Latitude": lat, "Longitude": lon})
        assert parse_aisstream(m, c, RECV) is None, (lat, lon)


def test_bad_mmsi_dropped() -> None:
    c = StaticCache()
    for mmsi in (0, 12345, 1234567890, 55):
        m = _pos("PositionReport", {"Latitude": 3.0, "Longitude": 101.0}, mmsi)
        m["MetaData"]["MMSI"] = mmsi
        assert parse_aisstream(m, c, RECV) is None, mmsi


def test_static_data_merged_into_later_positions() -> None:
    cache = StaticCache()
    msgs = _msgs()
    for m in msgs[:3]:  # ShipStaticData + two StaticDataReport parts
        assert parse_aisstream(m, cache, RECV) is None
    a = cache.get(563000001)
    assert a == {"name": "MV TESTER", "ship_type": 70, "destination": "SINGAPORE"}
    assert cache.get(538000002) == {"name": "FISHER ONE", "ship_type": 30}
    pos = parse_aisstream(msgs[3], cache, RECV)
    assert pos is not None and pos.name == "MV TESTER" and pos.ship_type == 70
    assert pos.destination == "SINGAPORE"


def test_unknown_types_and_malformed_ignored() -> None:
    c = StaticCache()
    injected = next(m for m in _msgs() if m.get("MessageType") == "SafetyBroadcastMessage")
    assert parse_aisstream(injected, c, RECV) is None
    assert parse_aisstream({"error": "nope"}, c, RECV) is None
    assert parse_aisstream({"MessageType": "PositionReport", "Message": {}}, c, RECV) is None


def test_time_parsing_incl_nanoseconds() -> None:
    t = parse_time_utc("2026-09-30 06:00:03.123456789 +0000 UTC")
    assert t == datetime(2026, 9, 30, 6, 0, 3, 123456, tzinfo=UTC)
    assert parse_time_utc("2026-09-30 06:00:16.5 +0000 UTC").microsecond == 500000
    assert parse_time_utc("2026-09-30 06:00:16 +0000 UTC").microsecond == 0
    assert parse_time_utc("garbage", RECV) == RECV
    assert parse_time_utc(None, RECV) == RECV
    o = parse_aisstream(
        _pos(
            "PositionReport",
            {"Latitude": 3.0, "Longitude": 100.8},
            t="2026-09-30 06:00:03.9 +0000 UTC",
        ),
        StaticCache(),
        RECV,
    )
    assert o is not None and o.ts.second == 3 and o.ts.tzinfo is not None


def test_ts_falls_back_to_receive_time() -> None:
    o = parse_aisstream(
        _pos("PositionReport", {"Latitude": 3.0, "Longitude": 100.8}), StaticCache(), RECV
    )
    assert o is not None and o.ts == RECV


def test_static_cache_ttl_and_lru() -> None:
    now = [0.0]
    c = StaticCache(ttl_s=100, max_size=2, clock=lambda: now[0])
    c.update(1, name="a")
    c.update(2, name="b")
    c.update(3, name="c")  # evicts 1 (least recently used)
    assert c.get(1) is None and c.get(2) == {"name": "b"} and len(c) == 2
    now[0] = 500.0
    assert c.get(2) is None and c.get(3) is None
