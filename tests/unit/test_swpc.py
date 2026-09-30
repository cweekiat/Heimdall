"""SWPC Kp + NOAA scales -> space_weather confounder GeoEvents (hand-built fixtures, offline)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import respx

from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.swpc import SwpcConnector, parse_swpc

FIX = Path(__file__).parent.parent / "fixtures" / "swpc"
BASE = "https://swpc.example.invalid"


def _load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def test_parse_kp_and_scales() -> None:
    events, dropped = parse_swpc(_load("kp.json"), _load("scales.json"))
    assert dropped == 1  # null Kp
    assert [e.value for e in events] == [2.33, 5.0, 6.67]
    e0, e1, e2 = events
    assert e0.kind == "space_weather" and e0.source == "swpc" and e0.attrs["role"] == "confounder"
    assert e0.lat is None and e0.lon is None
    assert e0.ts == datetime(2026, 9, 29, 0, 0, tzinfo=UTC) and e0.ts.tzinfo is not None
    assert isinstance(e0.attrs["kp"], float) and e0.attrs["g_scale"] == 0
    assert e1.attrs["g_scale"] == 1  # derived from Kp 5
    assert e2.attrs["g_scale"] == 3 and e2.attrs["r_scale"] == 1 and e2.attrs["s_scale"] == 0
    assert e0.attrs["r_scale"] is None


def test_parse_header_row_layout_and_no_scales() -> None:
    rows = [["time_tag", "Kp", "a_running"], ["2026-09-29 03:00:00.000", "4.00", "27"]]
    events, dropped = parse_swpc(rows)
    assert dropped == 0 and events[0].value == 4.0 and events[0].attrs["g_scale"] == 0
    assert parse_swpc({"not": "a list"}) == ([], 0)


def test_ids_stable() -> None:
    a = [e.id for e in parse_swpc(_load("kp.json"))[0]]
    assert a == [e.id for e in parse_swpc(_load("kp.json"))[0]] and len(set(a)) == 3


@respx.mock
async def test_poll_fetches_both_products() -> None:
    respx.get(f"{BASE}/products/noaa-planetary-k-index.json").mock(
        return_value=httpx.Response(200, json=_load("kp.json"))
    )
    respx.get(f"{BASE}/products/noaa-scales.json").mock(
        return_value=httpx.Response(200, json=_load("scales.json"))
    )
    c = SwpcConnector("swpc", BASE, client=RateLimitedClient("swpc", rps=1000, max_retries=0))
    events = await c.poll_once()
    assert len(events) == 3 and c.health().status == "ok"
    assert max(events, key=lambda e: e.ts).attrs["g_scale"] == 3


@respx.mock
async def test_scales_failure_keeps_kp() -> None:
    respx.get(f"{BASE}/products/noaa-planetary-k-index.json").mock(
        return_value=httpx.Response(200, json=_load("kp.json"))
    )
    respx.get(f"{BASE}/products/noaa-scales.json").mock(return_value=httpx.Response(404))
    c = SwpcConnector("swpc", BASE, client=RateLimitedClient("swpc", rps=1000, max_retries=0))
    assert len(await c.poll_once()) == 3
