"""GFW events: paging across two pages, ADR-005 section A mapping, 401 handling (respx, offline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx

from heimdall.config import AOI
from heimdall.ingest.gfw import GfwConnector, parse_gfw_events, parse_gfw_events_counted
from heimdall.ingest.http import RateLimitedClient
from heimdall.schemas import Domain, GeoEvent

FIX = Path(__file__).parent.parent / "fixtures" / "gfw"
BASE = "https://gateway.example.invalid/v3"
FAKE_CRED = "not-a-real-value-0001"
AOIS = [
    AOI(name="malacca", lat=3.0, lon=100.8, radius_nm=250),
    AOI(name="scs", lat=12.5, lon=113.5, radius_nm=250),
    AOI(name="taiwan", lat=22.5, lon=120.5, radius_nm=250),
]


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text())


def _conn(**kw: Any) -> GfwConnector:
    return GfwConnector(
        "gfw",
        BASE,
        AOIS,
        FAKE_CRED,
        client=RateLimitedClient("gfw", rps=1000, max_retries=0),
        **kw,
    )


def _dispatch(request: httpx.Request) -> httpx.Response:
    p = request.url.params
    ds = p["datasets[0]"]
    kind = next(k for k in ("gaps", "loitering", "encounters") if k in ds)
    page = 1 if int(p["offset"]) == 0 else 2
    return httpx.Response(200, json=_load(f"events_{kind}_p{page}.json"))


@respx.mock
async def test_paging_joins_both_pages() -> None:
    route = respx.get(f"{BASE}/events").mock(side_effect=_dispatch)
    c = _conn()
    events = await c.poll_once()
    by_id = {e.id: e for e in events}
    assert set(by_id) == {
        f"gfw:{t}-{i}"
        for t, ids in [("gap", (1, 2, 3)), ("loitering", (4, 5)), ("encounter", (6, 7))]
        for i in ids
    }
    assert route.call_count == 3 * 3 * 2  # AOIs x datasets x pages
    req = route.calls[0].request
    assert req.headers["Authorization"] == f"Bearer {FAKE_CRED}"
    assert c.n_dropped >= 1  # gap-bad (implausible position)
    assert c.health().status == "ok" and FAKE_CRED not in c.health().model_dump_json()


@respx.mock
async def test_401_marks_down_unauthorized() -> None:
    route = respx.get(f"{BASE}/events").mock(
        return_value=httpx.Response(401, json=_load("events_401.json"))
    )
    c = _conn()
    assert await c.poll_once() == []
    h = c.health()
    assert h.status == "down" and h.detail == "unauthorized"
    assert route.call_count == 1  # stops hammering after the first rejection


@respx.mock
async def test_recovers_after_unauthorized() -> None:
    respx.get(f"{BASE}/events").mock(return_value=httpx.Response(403, json={}))
    c = _conn()
    await c.poll_once()
    assert c.health().status == "down"
    respx.get(f"{BASE}/events").mock(side_effect=_dispatch)
    assert await c.poll_once()
    assert c.health().status == "ok"


@respx.mock
async def test_sar_only_when_enabled() -> None:
    def _sar(request: httpx.Request) -> httpx.Response:
        assert "sar" in request.url.params["datasets[0]"]
        return httpx.Response(200, json={"entries": [], "nextOffset": None})

    route = respx.get(f"{BASE}/events").mock(side_effect=_sar)
    await _conn(datasets=["sar"], sar=False).poll_once()
    assert route.call_count == 0
    await _conn(datasets=["sar"], sar=True).poll_once()
    assert route.call_count == len(AOIS)


def test_parse_matches_adr005_gap() -> None:
    evs = parse_gfw_events(_load("events_gaps_p1.json"), "gaps")
    assert [e.id for e in evs] == ["gfw:gap-1", "gfw:gap-2"]  # gap-bad dropped
    e = evs[0]
    assert isinstance(e, GeoEvent)
    assert (e.kind, e.domain, e.source) == ("gfw_gap", Domain.SEA, "gfw")
    assert e.valid_from == e.ts and e.valid_to is not None
    assert e.value == 8.0  # duration hours
    assert e.attrs["mmsi"] == 563000001 and e.attrs["gfw_vessel_id"] == "vid-1"
    assert e.h3_r4 and e.h3_r6
    assert parse_gfw_events_counted(_load("events_gaps_p1.json"), "gaps")[1] == 1


def test_parse_loitering_and_encounter() -> None:
    (lo,) = parse_gfw_events(_load("events_loitering_p1.json"), "loitering")
    assert lo.kind == "gfw_loitering" and lo.value == 6.0
    (en,) = parse_gfw_events(_load("events_encounters_p1.json"), "encounters")
    assert en.kind == "gfw_encounter" and en.attrs["partner_mmsi"] == 412000004
    assert en.value == 2.0  # end - start when no explicit duration
    (en2,) = parse_gfw_events(_load("events_encounters_p2.json"), "encounters")
    assert en2.attrs["partner_mmsi"] is None  # non-9-digit ssvid


def test_parse_sar_is_space_domain() -> None:
    payload = {
        "entries": [
            {
                "id": "s1",
                "start": "2026-09-28T00:00:00Z",
                "end": "2026-09-28T00:00:00Z",
                "position": {"lat": 10.0, "lon": 110.0},
                "vessel": {},
            }
        ]
    }
    (e,) = parse_gfw_events(payload, "sar")
    assert e.kind == "sar_detection" and e.domain == Domain.SPACE and e.attrs["mmsi"] is None
