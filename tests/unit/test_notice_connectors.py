from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from heimdall.config import Settings
from heimdall.ingest.china_msa import ChinaMsaConnector
from heimdall.ingest.china_msa import build as build_china
from heimdall.ingest.faa_nms import FaaNmsConnector
from heimdall.ingest.faa_nms import build as build_faa
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.nga_msi import NgaMsiConnector
from heimdall.ingest.nga_msi import build as build_nga
from heimdall.ingest.notice_base import load_warnings_cfg
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent

FIX = Path(__file__).resolve().parents[1] / "fixtures"
RULES = load_warnings_cfg()["categories"]
NGA_URL = "https://msi.nga.mil/api/publications/broadcast-warn"
MSA = "https://www.msa.gov.cn"
FAA = "https://nms.aim.faa.gov"


async def _nosleep(_: float) -> None:
    return None


def _client(name: str) -> RateLimitedClient:
    return RateLimitedClient(name, rps=1000, max_retries=0, sleep=_nosleep)


def _nga() -> NgaMsiConnector:
    return NgaMsiConnector(
        "https://msi.nga.mil/api",
        {"P": "HYDROPAC"},
        RULES,
        client=_client("nga_msi"),
        sleep=_nosleep,
    )


def _check_adr005(e: GeoEvent) -> None:
    a = e.attrs
    assert e.kind in ("nav_warning", "notam")
    assert a["category"] in {
        "military_exercise",
        "live_firing",
        "missile_rocket",
        "gnss_interference",
        "hazard",
        "other",
    }
    assert a["parse_status"] in {"ok", "partial", "no_geometry", "too_large"}
    assert a["status"] in {"active", "cancelled"}
    assert isinstance(a["h3_r4_cells"], list) and len(a["h3_r4_cells"]) <= 2000
    assert len(a["ref"]) <= 40 and all(
        (ch.isalnum() and ch.isupper()) or ch in "/ -" or ch.isdigit() for ch in a["ref"]
    )
    assert e.ts.tzinfo is not None
    assert e.text is None or len(e.text) <= 8000
    if a["parse_status"] in ("ok", "partial"):
        assert e.geometry and a["h3_r4_cells"] and e.lat is not None and e.h3_r4


@pytest.mark.asyncio
@respx.mock
async def test_nga_fixture_to_events() -> None:
    route = respx.get(NGA_URL).mock(
        return_value=httpx.Response(
            200, json=json.loads((FIX / "nga_msi/broadcast_warn_hydropac.json").read_text())
        )
    )
    c = _nga()
    events = await c.poll_once()
    assert route.called and route.calls[0].request.url.params["navArea"] == "P"
    by_id = {e.id: e for e in events}
    assert len(events) == 5 and c.n_dropped == 1
    for e in events:
        _check_adr005(e)
        assert e.domain == Domain.SEA and e.source == "nga_msi"

    poly = by_id["nga_msi:P:2026:1234"]
    assert poly.attrs["ref"] == "HYDROPAC 1234/26"
    assert poly.attrs["category"] == "military_exercise"
    assert (
        poly.geometry and poly.geometry["type"] == "Polygon" and poly.attrs["parse_status"] == "ok"
    )
    assert poly.valid_from == datetime(2026, 9, 30, 8, tzinfo=UTC)
    assert poly.valid_to == datetime(2026, 10, 2, 12, tzinfo=UTC)
    assert by_id["nga_msi:P:2026:1240"].attrs["category"] == "live_firing"
    assert by_id["nga_msi:P:2026:1250"].attrs["category"] == "gnss_interference"
    none = by_id["nga_msi:P:2026:1251"]
    assert none.attrs["parse_status"] == "no_geometry" and none.geometry is None

    cancelled = by_id["nga_msi:P:2026:1199"]
    assert cancelled.attrs["status"] == "cancelled"
    assert cancelled.valid_to == datetime(2026, 9, 28, 12, tzinfo=UTC)
    assert c.health().status == "ok" and c.health().records_last_hour == 5
    assert isinstance(c, Connector)


@pytest.mark.asyncio
@respx.mock
async def test_nga_ids_stable_repoll_and_revision() -> None:
    payload = json.loads((FIX / "nga_msi/broadcast_warn_hydropac.json").read_text())
    route = respx.get(NGA_URL).mock(return_value=httpx.Response(200, json=payload))
    first = await _nga().poll_once()
    c = _nga()
    again = await c.poll_once()
    assert [e.id for e in first] == [e.id for e in again]  # stable across runs
    assert await c.poll_once() == []  # nothing new
    # the exercise warning is cancelled upstream -> one revision with the same id
    row = payload["broadcast-warn"][0]
    row["status"] = "C"
    row["cancelDate"] = "011200Z OCT 2026"
    route.mock(return_value=httpx.Response(200, json=payload))
    rev = await c.poll_once()
    assert [e.id for e in rev] == ["nga_msi:P:2026:1234"]
    assert rev[0].attrs["status"] == "cancelled"
    assert rev[0].valid_to == datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.mark.asyncio
@respx.mock
async def test_nga_maintenance_html_is_down_and_backs_off() -> None:
    respx.get(NGA_URL).mock(
        return_value=httpx.Response(
            200,
            text="<html><body>MSI IS CURRENTLY UNDER MAINTENANCE</body></html>",
            headers={"content-type": "text/html"},
        )
    )
    c = _nga()
    assert await c.poll_once() == []
    h = c.health()
    assert h.status == "down" and h.detail == "source maintenance"
    assert "MAINTENANCE" not in (h.last_error or "")  # never echoes bodies
    d1 = c.next_delay()
    await c.poll_once()
    assert c.next_delay() > d1  # exponential backoff between polls
    assert c.client.breaker.failures == 2
    for _ in range(4):
        await c.poll_once()
    assert not c.client.breaker.allow()  # circuit opens; further polls make no request
    n = respx.calls.call_count
    await c.poll_once()
    assert respx.calls.call_count == n and c.health().detail == "circuit open"


@pytest.mark.asyncio
@respx.mock
async def test_nga_recovers_after_maintenance() -> None:
    payload = json.loads((FIX / "nga_msi/broadcast_warn_hydropac.json").read_text())
    route = respx.get(NGA_URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    c = _nga()
    await c.poll_once()
    route.mock(return_value=httpx.Response(200, json=payload))
    assert len(await c.poll_once()) == 5
    assert c.health().status == "ok"


def _china(enabled: bool = True) -> ChinaMsaConnector:
    return ChinaMsaConnector(
        MSA, {"sansha": "CH1"}, RULES, client=_client("china_msa"), enabled=enabled, sleep=_nosleep
    )


def _mock_msa() -> respx.Route:
    lst = (FIX / "china_msa/list_sansha.html").read_text()
    respx.get(f"{MSA}/msacncms_wap/pages/info_detail.do", params={"id": "EX2026093001"}).mock(
        return_value=httpx.Response(200, text=(FIX / "china_msa/detail_exercise.html").read_text())
    )
    respx.get(f"{MSA}/msacncms_wap/pages/info_detail.do", params={"id": "CI2026092902"}).mock(
        return_value=httpx.Response(200, text=(FIX / "china_msa/detail_circle.html").read_text())
    )
    return respx.get(f"{MSA}/msacncms_wap/pages/info_warn.do").mock(
        return_value=httpx.Response(200, text=lst)
    )


@pytest.mark.asyncio
@respx.mock
async def test_china_fixtures_to_events() -> None:
    lst = _mock_msa()
    c = _china()
    events = await c.poll_once()
    assert lst.calls[0].request.url.params["channelId"] == "CH1"
    assert (
        respx.calls.call_count == 3
    )  # listing + 2 detail pages; off-host/non-detail never fetched
    by_id = {e.id: e for e in events}
    assert set(by_id) == {"china_msa:EX2026093001", "china_msa:CI2026092902"}
    for e in events:
        _check_adr005(e)
        assert e.attrs["lang"] == "zh" and e.domain == Domain.SEA
    ex = by_id["china_msa:EX2026093001"]
    assert ex.attrs["category"] == "military_exercise"
    assert ex.geometry and ex.geometry["type"] == "Polygon" and ex.attrs["parse_status"] == "ok"
    assert ex.valid_from == datetime(2026, 9, 30, 0, tzinfo=UTC)  # 08:00 UTC+8
    assert ex.valid_to == datetime(2026, 9, 30, 10, tzinfo=UTC)
    ci = by_id["china_msa:CI2026092902"]
    assert ci.attrs["category"] == "live_firing" and ci.geometry
    assert ci.valid_from == datetime(2026, 9, 28, 22, tzinfo=UTC)
    # re-poll: nothing new and no detail refetch
    assert await c.poll_once() == []
    assert respx.calls.call_count == 4


@pytest.mark.asyncio
@respx.mock
async def test_china_geo_block_403() -> None:
    respx.get(f"{MSA}/msacncms_wap/pages/info_warn.do").mock(
        return_value=httpx.Response(403, text="denied")
    )
    c = _china()
    assert await c.poll_once() == []
    h = c.health()
    assert h.status == "down" and h.detail == "geo-blocked"


@pytest.mark.asyncio
@respx.mock
async def test_china_disabled_makes_no_requests() -> None:
    c = _china(enabled=False)
    assert c.health().status == "disabled"
    assert [b async for b in c.stream()] == []
    assert respx.calls.call_count == 0


def test_build_factories_default_to_config() -> None:
    s = Settings()
    n = build_nga("nga_msi", {"base": "https://msi.nga.mil/api", "reliability": "A"}, s)
    assert set(n.navareas) == {"P", "11"} and n.domain == Domain.SEA
    c = build_china("china_msa", {"base": MSA, "enabled": False}, s)
    assert c.health().status == "disabled" and c.channels


def _faa(key: str | None = "k", secret: str | None = "s") -> FaaNmsConnector:
    return FaaNmsConnector(
        FAA, ["VYYF"], RULES, key=key, secret=secret, client=_client("faa"), sleep=_nosleep
    )


def _mock_faa() -> tuple[respx.Route, respx.Route]:
    tok = respx.post(f"{FAA}/v1/auth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "T0K"})
    )
    n = respx.get(f"{FAA}/nmsapi/v1/notams").mock(
        return_value=httpx.Response(
            200, json=json.loads((FIX / "faa_nms/notams_sample.json").read_text())
        )
    )
    return tok, n


@pytest.mark.asyncio
@respx.mock
async def test_faa_fixture_to_events() -> None:
    tok, notams = _mock_faa()
    c = _faa()
    events = await c.poll_once()
    assert tok.called and notams.calls[0].request.headers["authorization"] == "Bearer T0K"
    assert notams.calls[0].request.url.params["icaoLocation"] == "VYYF"
    by_ref = {e.attrs["ref"]: e for e in events}
    assert len(events) == 5 and c.n_dropped == 1
    for e in events:
        _check_adr005(e)
        assert e.domain == Domain.AIR and e.kind == "notam" and e.source == "faa_nms_notam"

    circ = by_ref["NOTAM A0101/26"]
    assert circ.attrs["alt_lower_ft"] == 0 and circ.attrs["alt_upper_ft"] == 25000
    assert circ.geometry and len(circ.geometry["coordinates"][0]) == 33
    assert circ.attrs["category"] == "military_exercise"
    assert circ.valid_from == datetime(2026, 9, 30, 8, tzinfo=UTC)
    poly = by_ref["NOTAM B0202/26"]
    assert poly.attrs["category"] == "missile_rocket" and len(poly.geometry["coordinates"][0]) == 5
    perm = by_ref["NOTAM C0303/26"]
    assert perm.valid_to is None and perm.attrs["permanent"] is True
    assert perm.attrs["category"] == "hazard" and perm.attrs["parse_status"] == "ok"
    gnss = by_ref["NOTAM D0404/26"]
    assert gnss.attrs["category"] == "gnss_interference" and gnss.attrs["parse_status"] == "ok"
    assert by_ref["NOTAM A0102/26"].attrs["status"] == "cancelled"
    assert await c.poll_once() == []


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(("key", "secret"), [(None, "s"), ("k", None), ("", ""), (None, None)])
async def test_faa_key_gating_no_requests(key: str | None, secret: str | None) -> None:
    c = _faa(key, secret)
    h = c.health()
    assert h.status == "needs_key" and "FAA_NMS_KEY" in (h.detail or "")
    assert h.detail != "k"
    assert await c.poll_once() == []
    assert [b async for b in c.stream()] == []
    assert respx.calls.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_faa_build_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAA_NMS_KEY", raising=False)
    monkeypatch.setenv("FAA_NMS_SECRET", "shh")
    c = build_faa("faa_nms_notam", {"base": FAA, "rps": 1000}, Settings())
    assert c.health().status == "needs_key" and "shh" not in (c.health().detail or "")
    assert respx.calls.call_count == 0
    monkeypatch.setenv("FAA_NMS_KEY", "abc")
    _mock_faa()
    c2 = build_faa("faa_nms_notam", {"base": FAA, "rps": 1000}, Settings())
    assert c2.health().status == "ok" and {"VYYF", "EPWW"} <= set(c2.firs)
    assert len(await c2.poll_once()) >= 5


@pytest.mark.asyncio
@respx.mock
async def test_faa_bad_credentials_are_unauthorized() -> None:
    respx.post(f"{FAA}/v1/auth/token").mock(return_value=httpx.Response(401, json={"error": "x"}))
    c = _faa()
    assert await c.poll_once() == []
    assert c.health().status == "down" and c.health().detail == "unauthorized"
