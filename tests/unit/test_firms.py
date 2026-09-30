"""FIRMS area CSV -> thermal GeoEvents (hand-built fixtures, respx, offline)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx

from heimdall.config import AOI
from heimdall.ingest.firms import FirmsConnector, normalise_confidence, parse_firms_csv
from heimdall.ingest.http import RateLimitedClient
from heimdall.schemas import KEY_CONNECTORS

FIX = Path(__file__).parent.parent / "fixtures" / "firms"
BASE = "https://firms.example.invalid/api"
FAKE_CRED = "not-a-real-value-0002"
AOIS = [AOI(name="myanmar", lat=20.5, lon=96.5, radius_nm=250)]


def _conn(token: str | None = FAKE_CRED, **kw: object) -> FirmsConnector:
    return FirmsConnector(
        "firms",
        BASE,
        AOIS,
        token,
        client=RateLimitedClient("firms", rps=1000, max_retries=0),
        **kw,  # type: ignore[arg-type]
    )


def test_parse_viirs_dedup_drop_and_fields() -> None:
    events, dropped = parse_firms_csv((FIX / "area_viirs.csv").read_text())
    assert len(events) == 3  # duplicate collapsed
    assert dropped == 2  # negative FRP, latitude 95
    e = next(x for x in events if x.value == 12.4)
    assert e.kind == "thermal" and e.source == "firms" and e.attrs["role"] == "signal"
    assert e.ts == datetime(2026, 9, 29, 1, 30, tzinfo=UTC)  # acq_date + acq_time (HHMM) -> UTC
    assert e.attrs["confidence"] == "nominal" and e.attrs["satellite"] == "N"
    assert e.attrs["bright_ti4"] == 345.2 and e.attrs["bright_ti5"] == 301.5
    assert e.attrs["daynight"] == "N" and e.attrs["version"] == "2.0NRT"
    assert e.h3_r4 and e.h3_r6
    assert {x.attrs["confidence"] for x in events} == {"nominal", "low", "high"}


def test_ids_stable_across_reparse() -> None:
    text = (FIX / "area_viirs.csv").read_text()
    a = sorted(e.id for e in parse_firms_csv(text)[0])
    assert a == sorted(e.id for e in parse_firms_csv(text)[0])
    assert a[0].startswith("firms|")


def test_modis_row_and_confidence_map() -> None:
    events, dropped = parse_firms_csv((FIX / "area_modis.csv").read_text())
    assert dropped == 0
    by_conf = {e.attrs["confidence"]: e for e in events}
    assert set(by_conf) == {"high", "low"}  # 85 -> high, 20 -> low
    assert by_conf["high"].attrs["bright_ti4"] == 320.5 and by_conf["high"].value == 25.0
    assert [normalise_confidence(v) for v in ("29", "30", "79", "80", "h", "x", "101")] == [
        "low",
        "nominal",
        "nominal",
        "high",
        "high",
        None,
        None,
    ]


@respx.mock
async def test_poll_builds_urls_and_filters_to_aoi() -> None:
    route = respx.get(url__regex=rf"{BASE}/area/csv/.+").mock(
        return_value=httpx.Response(200, text=(FIX / "area_viirs.csv").read_text())
    )
    c = _conn(products=["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT"])
    events = await c.poll_once()
    assert route.call_count == 2
    assert f"/area/csv/{FAKE_CRED}/VIIRS_SNPP_NRT/" in str(route.calls[0].request.url)
    assert len(events) == 3  # same rows from both products collapse by id
    assert c.n_dropped == 4 and c.health().status == "ok"


@respx.mock
async def test_missing_key_needs_key_and_no_request() -> None:
    route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, text=""))
    c = _conn(token=None)
    assert await c.poll_once() == []
    assert [b async for b in c.stream()] == []
    assert not route.called
    h = c.health()
    assert h.status == "needs_key" and h.detail == "set FIRMS_MAP_KEY"


@respx.mock
async def test_error_does_not_leak_key() -> None:
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(500))
    c = _conn(products=["VIIRS_SNPP_NRT"])
    assert await c.poll_once() == []
    h = c.health()
    assert h.status == "degraded" and FAKE_CRED not in (h.last_error or "")


def test_key_registered_for_ui() -> None:
    assert KEY_CONNECTORS["FIRMS_MAP_KEY"] == "firms"
