"""ADR-004 key gating: without the env key nothing is requested and health says needs_key."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from heimdall.config import Settings, settings, sources
from heimdall.ingest.registry import build_connectors
from heimdall.ports import Connector

CASES = [("aisstream", "AISSTREAM_KEY"), ("gfw", "GFW_TOKEN")]


def _build(name: str) -> Connector:
    conns = build_connectors(settings(), sources(), enabled={name})
    assert [c.name for c in conns] == [name]
    return conns[0]


async def _drain(c: Connector) -> list[Any]:
    out: list[Any] = []
    async for b in c.stream():
        out.extend(b)
    return out


@pytest.mark.parametrize(("name", "env"), CASES)
@respx.mock
async def test_absent_key_makes_no_requests(
    name: str, env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(env, raising=False)
    http = respx.route().mock(return_value=httpx.Response(200, json={}))
    calls: list[str] = []

    def _no_connect(url: str) -> Any:
        calls.append(url)
        raise AssertionError("connect must not be called without a key")

    c = _build(name)
    if name == "aisstream":
        c.connect = _no_connect  # type: ignore[attr-defined]
    assert c.health().status == "needs_key"
    assert env in (c.health().detail or "")
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await asyncio.wait_for(_drain(c), timeout=0.1) == []
    assert loop.time() - t0 < 0.1
    assert not http.called and calls == []
    await c.aclose()


@pytest.mark.parametrize(("name", "env"), CASES)
def test_empty_key_counts_as_absent(name: str, env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env, "")
    assert _build(name).health().status == "needs_key"


@pytest.mark.parametrize(("name", "env"), CASES)
def test_present_key_is_not_needs_key_and_never_in_health(
    name: str, env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env, "s3cret-value-xyz")
    h = _build(name).health()
    assert h.status != "needs_key" and "s3cret" not in h.model_dump_json()


def test_registry_builds_both_from_default_sources() -> None:
    names = [c.name for c in build_connectors(Settings(), sources())]
    assert "aisstream" in names and "gfw" in names
