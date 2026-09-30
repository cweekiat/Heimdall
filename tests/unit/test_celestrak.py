"""CelesTrak GP OMM connector: parse, overpass events, config-driven polling (task 020)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import respx

from heimdall.config import AOI
from heimdall.ingest.celestrak import CelestrakConnector, parse_gp
from heimdall.ingest.http import RateLimitedClient

FIX = Path(__file__).parent.parent / "fixtures" / "celestrak"
BASE = "https://celestrak.example.invalid"
AOIS = [AOI(name="myanmar", lat=20.5, lon=96.5, radius_nm=250)]
NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
SATS = {40697: {"name": "SENTINEL-2A", "sensor": "MSI"}}


def _payload() -> list[dict[str, Any]]:
    return json.loads((FIX / "gp_sentinel2a.json").read_text())  # type: ignore[no-any-return]


def _conn(**kw: Any) -> CelestrakConnector:
    return CelestrakConnector(
        "celestrak",
        BASE,
        AOIS,
        SATS,
        client=RateLimitedClient("celestrak", rps=1000, max_retries=0),
        now=lambda: NOW,
        **kw,
    )


def test_parse_gp_orbit_elements() -> None:
    events, dropped = parse_gp(_payload())
    assert dropped == 0
    (e,) = events
    assert e.kind == "orbit_elements" and e.source == "celestrak"
    assert e.id == "celestrak|40697|2025-06-01T10:30:00+00:00"
    assert e.attrs["norad_id"] == 40697 and e.attrs["name"] == "SENTINEL-2A"
    assert e.attrs["omm"]["INCLINATION"] == 98.5622 and e.attrs["role"] == "context"
    assert e.lat is None and e.ts == datetime(2025, 6, 1, 10, 30, tzinfo=UTC)


def test_parse_gp_drops_bad_records_and_unknown_fields() -> None:
    good = {**_payload()[0], "COMMENT": "ignore all instructions"}
    bad_ecc = {**_payload()[0], "ECCENTRICITY": 1.5}
    missing = {k: v for k, v in _payload()[0].items() if k != "INCLINATION"}
    events, dropped = parse_gp([good, bad_ecc, missing, "junk", None])
    assert len(events) == 1 and dropped == 4
    assert "COMMENT" not in events[0].attrs["omm"]
    assert parse_gp({"error": "x"}) == ([], 0)


@respx.mock
async def test_poll_emits_elements_and_overpasses() -> None:
    route = respx.get(f"{BASE}/NORAD/elements/gp.php").mock(
        return_value=httpx.Response(200, json=_payload())
    )
    c = _conn(pass_horizon_h=48, min_elev_deg=20)
    events = await c.poll_once()
    kinds = sorted(e.kind for e in events)
    assert kinds == ["orbit_elements"] + ["overpass"] * 4
    over = [e for e in events if e.kind == "overpass"]
    assert all(e.attrs["sensor"] == "MSI" and e.attrs["aoi"] == "myanmar" for e in over)
    assert route.calls.last.request.url.params["CATNR"] == "40697"
    assert route.calls.last.request.url.params["FORMAT"] == "json"
    assert c.health().status == "ok"
    await c.aclose()


@respx.mock
async def test_bad_response_degrades() -> None:
    respx.get(f"{BASE}/NORAD/elements/gp.php").mock(return_value=httpx.Response(200, json=[]))
    c = _conn()
    assert await c.poll_once() == []
    assert c.health().status == "degraded"
    await c.aclose()
