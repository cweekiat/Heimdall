"""OpenSky global connector: parsing, OAuth, pacing from headers, 429, key gating."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from heimdall.config import AOI
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.opensky import (
    TOKEN_URL,
    OpenSkyConnector,
    next_delay,
    parse_states,
    seconds_to_utc_reset,
)

FIX = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "opensky" / "states_all.json").read_text()
)
BASE = "https://opensky-network.org/api"
SINGAPORE = AOI(name="sg", lat=1.3, lon=103.8, radius_nm=100)
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def test_parse_states_nulls_and_drops() -> None:
    rows, dropped = parse_states(FIX)
    assert {r.hex for r in rows} == {"71be18", "a1b2c3", "76ce01"}
    assert dropped == 4  # no position, Mach>2, null island, short row
    kal = next(r for r in rows if r.hex == "71be18")
    assert kal.is_global and kal.provider == "opensky" and not kal.mil
    assert kal.flight == "KAL123" and kal.alt_baro == 35000 and round(kal.gs or 0) == 476
    assert kal.nic is None and kal.nac_p is None and not kal.is_true_adsb
    gnd = next(r for r in rows if r.hex == "a1b2c3")
    assert gnd.on_ground and gnd.alt_baro is None and gnd.flight is None and gnd.track is None


def test_next_delay_bounds_and_budget() -> None:
    assert next_delay(None, 1000, poll_s=90) == 90
    assert next_delay(4000, 86400) == pytest.approx(86.4)
    assert next_delay(10**6, 100) == 10.0  # never faster than the data resolution
    assert next_delay(3, 3600) == 300.0  # cannot afford a call: slowest pace
    for rem in (4, 400, 3996, 4000):
        for reset in (60.0, 3600.0, 86400.0):
            d = next_delay(rem, reset)
            assert 10.0 <= d <= 300.0
            if 10.0 < d < 300.0:  # calls fitting into the reset window stay within the budget
                assert reset / d <= rem // 4 + 1e-9


def test_reset_seconds() -> None:
    assert seconds_to_utc_reset(NOW) == 12 * 3600


def _conn() -> OpenSkyConnector:
    async def _sleep(s: float) -> None:
        return None

    return OpenSkyConnector(
        "opensky_global",
        BASE,
        [SINGAPORE],
        "cid",
        "csecret",
        client=RateLimitedClient("opensky_global", rps=100, max_retries=0),
        sleep=_sleep,
        now=lambda: NOW,
    )


@respx.mock
async def test_poll_pacing_from_headers_and_aoi_skip() -> None:
    tok = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok123", "expires_in": 1800})
    )
    states = respx.get(f"{BASE}/states/all").mock(
        return_value=httpx.Response(200, json=FIX, headers={"X-Rate-Limit-Remaining": "2000"})
    )
    c = _conn()
    rows, wait = await c.poll_once()
    assert states.calls.last.request.headers["Authorization"] == "Bearer tok123"
    assert {r.hex for r in rows} == {"71be18", "a1b2c3"}  # Singapore row is inside an AOI
    assert c.n_in_aoi == 1 and c.credits_left == 2000
    assert wait == pytest.approx(43200 / 500)  # 500 calls left over 12 h
    assert "2000 credits left" in (c.health().detail or "")
    await c.poll_once()
    assert tok.call_count == 1  # token cached until expiry
    await c.aclose()


@respx.mock
async def test_429_honours_retry_after() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.get(f"{BASE}/states/all").mock(
        return_value=httpx.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "1234"})
    )
    c = _conn()
    rows, wait = await c.poll_once()
    assert rows == [] and wait == 1234 and c.n_err == 1
    assert c.health().status == "degraded"
    await c.aclose()


@respx.mock
async def test_token_failure_is_redacted_and_no_crash() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))
    c = _conn()
    rows, wait = await c.poll_once()
    assert rows == [] and wait == 300.0 and "csecret" not in (c.last_error or "")
    await c.aclose()


@respx.mock
async def test_needs_key_makes_no_request() -> None:
    c = OpenSkyConnector("opensky_global", BASE, [], None, None)
    assert [b async for b in c.stream()] == []
    assert c.health().status == "needs_key"
    assert respx.calls.call_count == 0
    await c.aclose()
