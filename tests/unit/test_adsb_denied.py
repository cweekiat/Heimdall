"""An aggregator that answers 401/403 stops polling and reports needs_key, not a crash."""

from __future__ import annotations

import asyncio

import httpx

from heimdall.ingest.adsb import AdsbAggregatorConnector
from heimdall.ingest.http import RateLimitedClient
from heimdall.config import AOI


def test_403_stops_polling_and_reports_needs_key() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, request=req)

    client = RateLimitedClient("t", rps=1000, transport=httpx.MockTransport(handler))
    aoi = AOI(name="x", lat=1.0, lon=100.0, radius_nm=100)
    conn = AdsbAggregatorConnector(
        "airplanes_live",
        "https://api.example",
        "/v2/point/{lat}/{lon}/{radius}",
        [aoi],
        client=client,
        idle_sleep_s=0,
    )

    async def run() -> int:
        n = 0
        async for _ in conn.stream():
            n += 1
        return n

    assert asyncio.run(asyncio.wait_for(run(), 5)) == 0
    assert calls == 1
    h = conn.health()
    assert h.status == "needs_key" and "403" in (h.detail or "")
