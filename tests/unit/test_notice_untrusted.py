"""Notice text is untrusted data: stored inert, never followed, bounded work."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest
import respx

from heimdall.config import Settings
from heimdall.ingest.china_msa import ChinaMsaConnector
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.nga_msi import NgaMsiConnector
from heimdall.ingest.notice_base import load_warnings_cfg
from heimdall.ingest.notice_parse import parse_coords, parse_shape, sanitize
from heimdall.schemas import GeoEvent

FIX = Path(__file__).resolve().parents[1] / "fixtures"
RULES = load_warnings_cfg()["categories"]
MSA = "https://www.msa.gov.cn"
REF_OK = re.compile(r"^[A-Z0-9/ -]{0,40}$")


async def _nosleep(_: float) -> None:
    return None


def _client(name: str) -> RateLimitedClient:
    return RateLimitedClient(name, rps=1000, max_retries=0, sleep=_nosleep)


def _assert_inert(e: GeoEvent) -> None:
    for field in (e.title, e.text):
        assert field is None or ("<" not in field and ">" not in field)
        assert field is None or len(field) <= 8000
    assert e.text and "<script" not in e.text.lower()
    assert REF_OK.match(e.attrs["ref"])
    # trusted structured fields never carry free text
    for k in ("category", "parse_status", "status"):
        assert e.attrs[k] in {
            "military_exercise",
            "live_firing",
            "missile_rocket",
            "gnss_interference",
            "hazard",
            "other",
            "ok",
            "partial",
            "no_geometry",
            "too_large",
            "active",
            "cancelled",
        }
    for v in e.attrs.values():
        if isinstance(v, str):
            assert ("evil" not in v.lower() and "ignore" not in v.lower()) or v == e.attrs["ref"]


@pytest.mark.asyncio
@respx.mock
async def test_injection_json_is_inert_and_no_extra_http() -> None:
    payload = json.loads((FIX / "notices/injection.json").read_text())
    route = respx.get("https://msi.nga.mil/api/publications/broadcast-warn").mock(
        return_value=httpx.Response(200, json=payload)
    )
    env_before, settings_before = dict(os.environ), Settings().model_dump()
    c = NgaMsiConnector(
        "https://msi.nga.mil/api",
        {"P": "HYDROPAC"},
        RULES,
        client=_client("nga_msi"),
        sleep=_nosleep,
    )
    t0 = time.perf_counter()
    events = await c.poll_once()
    assert time.perf_counter() - t0 < 1.0
    assert len(events) == 1
    e = events[0]
    _assert_inert(e)
    assert e.attrs["category"] == "live_firing"
    assert "IGNORE PREVIOUS INSTRUCTIONS" in (e.text or "")  # kept verbatim as data, not acted on
    assert route.call_count == 1  # the evil URL in the text was never requested
    assert dict(os.environ) == env_before and Settings().model_dump() == settings_before
    assert Settings().model_dump() == settings_before


@pytest.mark.asyncio
@respx.mock
async def test_injection_html_is_inert_and_links_not_followed() -> None:
    listing = (
        '<a href="/msacncms_wap/pages/info_detail.do?id=INJ1">jp</a>'
        '<a href="https://evil.example/click">x</a>'
        '<a href="//evil.example/info_detail.do?id=X">y</a>'
    )
    lst = respx.get(f"{MSA}/msacncms_wap/pages/info_warn.do").mock(
        return_value=httpx.Response(200, text=listing)
    )
    det = respx.get(f"{MSA}/msacncms_wap/pages/info_detail.do", params={"id": "INJ1"}).mock(
        return_value=httpx.Response(200, text=(FIX / "notices/injection.html").read_text())
    )
    c = ChinaMsaConnector(
        MSA, {"sansha": "CH1"}, RULES, client=_client("china_msa"), sleep=_nosleep
    )
    t0 = time.perf_counter()
    events = await c.poll_once()
    assert time.perf_counter() - t0 < 1.0
    assert lst.called and det.called
    assert respx.calls.call_count == 2  # nothing else (unmatched routes would raise)
    assert len(events) == 1
    e = events[0]
    _assert_inert(e)
    assert "fetch(" not in (e.text or "") and "alert(1)" not in (e.text or "")
    assert e.attrs["category"] == "military_exercise" and e.geometry


def test_pathological_input_bounded() -> None:
    raw = (FIX / "notices/injection.html").read_text()
    t0 = time.perf_counter()
    text = sanitize(raw, html=True)
    parse_coords(text)
    parse_shape(text)
    parse_shape(raw)  # even unsanitized raw input is bounded
    assert time.perf_counter() - t0 < 1.0
    assert len(text) <= 8000
    assert "<script" not in text
