"""Open-Meteo live forecast -> weather confounder GeoEvents (hand-built fixture, respx, offline)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx

from heimdall.config import AOI
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.open_meteo import OpenMeteoConnector, grid_points, parse_open_meteo

FIX = Path(__file__).parent.parent / "fixtures" / "open_meteo"
BASE = "https://weather.example.invalid/v1"
AOIS = [AOI(name="myanmar", lat=20.5, lon=96.5, radius_nm=60)]


def _payload() -> list[dict]:
    return json.loads((FIX / "forecast.json").read_text())


def test_parse_confounder_units_and_null_hours() -> None:
    events, dropped = parse_open_meteo(_payload())
    assert dropped == 0
    assert len(events) == 3  # null 01:00 hour skipped
    e = next(x for x in events if x.value == 100.0)
    assert e.kind == "weather" and e.source == "open_meteo" and e.attrs["role"] == "confounder"
    assert e.attrs["wind_speed_ms"] == 5.0  # 18 km/h -> m/s
    assert e.attrs["precipitation_mm"] == 2.5 and e.attrs["weather_code"] == 61
    assert e.attrs["grid_lat"] == 20.5 and e.attrs["grid_lon"] == 96.5
    assert e.ts == datetime(2026, 9, 29, 2, tzinfo=UTC) and e.value == e.attrs["cloud_cover"]
    assert len({x.id for x in events}) == 3
    assert parse_open_meteo(_payload())[0][0].id == events[0].id  # stable ids


def test_parse_handles_single_object_and_bad_input() -> None:
    assert len(parse_open_meteo(_payload()[1])[0]) == 1
    p = _payload()[1]
    p["hourly_units"]["wind_speed_10m"] = "furlongs"
    assert parse_open_meteo(p) == ([], 1)
    assert parse_open_meteo(["junk"]) == ([], 1)  # type: ignore[list-item]


def test_grid_points_snap_to_grid_inside_aoi() -> None:
    pts = grid_points(AOIS, 0.5)
    assert pts and all(round(a / 0.5) * 0.5 == a and round(b / 0.5) * 0.5 == b for a, b in pts)
    assert (20.5, 96.5) in pts and (30.0, 96.5) not in pts


@respx.mock
async def test_poll_uses_forecast_endpoint_with_past_days() -> None:
    route = respx.get(f"{BASE}/forecast").mock(return_value=httpx.Response(200, json=_payload()))
    c = OpenMeteoConnector(
        "open_meteo",
        BASE,
        AOIS,
        client=RateLimitedClient("open_meteo", rps=1000, max_retries=0),
        now=lambda: datetime(2026, 9, 30, tzinfo=UTC),
    )
    events = await c.poll_once()
    assert route.called
    q = route.calls[0].request.url.params
    assert q["past_days"] == "2" and q["forecast_days"] == "1"
    assert q["hourly"] == "cloud_cover,precipitation,wind_speed_10m,weather_code"
    assert len(events) == 3 and c.health().status == "ok"
    assert all(e.domain.value == "events" for e in events)
