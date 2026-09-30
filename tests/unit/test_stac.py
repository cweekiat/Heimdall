"""Sentinel STAC footprints: metadata only, assets never read or fetched (task 020, ADR-007 s3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import respx

from heimdall.config import AOI
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.stac import StacConnector, parse_stac_items

FIX = Path(__file__).parent.parent / "fixtures" / "stac"
BASE = "https://stac.example.invalid/v1"
AOIS = [AOI(name="myanmar", lat=20.5, lon=96.5, radius_nm=250)]
NOW = datetime(2025, 6, 3, tzinfo=UTC)


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text())


def _conn(**kw: Any) -> StacConnector:
    return StacConnector(
        "stac",
        BASE,
        AOIS,
        client=RateLimitedClient("stac", rps=1000, max_retries=0),
        now=lambda: NOW,
        **kw,
    )


def test_parse_s2_drops_assets_and_keeps_geometry() -> None:
    events, dropped = parse_stac_items(_load("search_s2.json"))
    assert dropped == 1  # BAD_NO_DATETIME
    assert [e.id for e in events] == [
        "stac|S2A_47QMB_20250601_0_L2A",
        "stac|S2B_47QNB_20250602_0_L2A",
    ]
    e = events[0]
    assert e.kind == "imagery_footprint" and e.source == "stac"
    assert e.geometry == _load("search_s2.json")["features"][0]["geometry"]
    assert e.value == 12.5
    assert e.url == (
        "https://stac.example.invalid/v1/collections/sentinel-2-l2a/items/S2A_47QMB_20250601_0_L2A"
    )
    assert e.attrs == {
        "role": "context",
        "collection": "sentinel-2-l2a",
        "platform": "sentinel-2a",
        "stac_item_id": "S2A_47QMB_20250601_0_L2A",
        "orbit_state": "descending",
        "relative_orbit": 75,
        "cloud_cover": 12.5,
    }
    assert e.ts == datetime(2025, 6, 1, 3, 45, 21, 24000, tzinfo=UTC)
    assert e.h3_r4 and e.h3_r6


def test_no_asset_data_anywhere_in_the_event() -> None:
    for name in ("search_s2.json", "search_s1.json"):
        events, _ = parse_stac_items(_load(name))
        dump = json.dumps([e.model_dump(mode="json") for e in events])
        for needle in ("thumbs.example.invalid", "cogs.example.invalid", "preview.jpg", "TCI.tif"):
            assert needle not in dump
        assert "assets" not in dump and "thumbnail" not in dump
        assert "IGNORE PREVIOUS" not in dump  # free-text property titles are not carried


def test_parse_s1() -> None:
    (e,), dropped = parse_stac_items(_load("search_s1.json"))
    assert dropped == 0
    assert e.attrs["platform"] == "sentinel-1a" and e.attrs["orbit_state"] == "ascending"
    assert e.attrs["relative_orbit"] == 143 and e.attrs["cloud_cover"] is None and e.value is None


@respx.mock
async def test_poll_posts_search_and_never_fetches_assets() -> None:
    route = respx.post(f"{BASE}/search").mock(
        return_value=httpx.Response(200, json=_load("search_s2.json"))
    )
    other = respx.route().mock(return_value=httpx.Response(200))  # any asset GET would land here
    c = _conn(lookback_days=10, max_items=500)
    events = await c.poll_once()
    assert {e.id for e in events} == {
        "stac|S2A_47QMB_20250601_0_L2A",
        "stac|S2B_47QNB_20250602_0_L2A",
    }
    body = json.loads(route.calls.last.request.content)
    assert body["collections"] == ["sentinel-2-l2a", "sentinel-1-grd"]
    assert body["datetime"] == "2025-05-24T00:00:00Z/2025-06-03T00:00:00Z"
    w, s, e, n = body["bbox"]
    assert w < 96.5 < e and s < 20.5 < n
    assert other.call_count == 0
    assert c.health().status == "ok" and c.n_dropped == 1
    await c.aclose()


@respx.mock
async def test_pagination_capped_and_same_host_only() -> None:
    page = _load("search_s2.json")
    page["links"] = [
        {
            "rel": "next",
            "method": "POST",
            "href": f"{BASE}/search",
            "body": {"token": "p2"},
            "merge": True,
        }
    ]
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=page)

    respx.post(f"{BASE}/search").mock(side_effect=handler)
    c = _conn(max_items=6)
    events = await c.poll_once()
    assert len(calls) == 2  # 3 raw features per page, stops once >= 6 seen
    assert calls[1]["token"] == "p2" and calls[1]["collections"]  # merged body
    assert len(events) == 2  # deduped by id
    await c.aclose()

    page["links"][0]["href"] = "https://evil.example.invalid/search"
    calls.clear()
    c = _conn(max_items=500)
    await c.poll_once()
    assert len(calls) == 1  # foreign-host next link is not followed
    await c.aclose()


@respx.mock
async def test_http_error_degrades_health() -> None:
    respx.post(f"{BASE}/search").mock(return_value=httpx.Response(400))
    c = _conn()
    assert await c.poll_once() == []
    assert c.health().status == "degraded"
    await c.aclose()
