"""Adaptive rate: the client halves its own rate on HTTP 429 and recovers slowly on success."""

from __future__ import annotations

import httpx
import pytest
import respx

from heimdall.ingest.http import RateLimitedClient


@respx.mock
async def test_429_halves_rate_then_success_recovers_towards_cap() -> None:
    t = [0.0]

    async def sleep(s: float) -> None:
        t[0] += s

    respx.get("https://x.test/a").mock(
        side_effect=[httpx.Response(429), httpx.Response(429), httpx.Response(200, json={})]
    )
    c = RateLimitedClient("t", rps=1.0, max_retries=4, sleep=sleep, clock=lambda: t[0])
    await c.get("https://x.test/a")
    assert c.bucket.rate == pytest.approx(0.25 * 1.05)
    for _ in range(100):  # never exceeds the configured rate
        c.bucket.rate = min(c.max_rps, c.bucket.rate * 1.05)
    assert c.bucket.rate == 1.0
