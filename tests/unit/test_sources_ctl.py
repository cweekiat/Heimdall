"""SourceManager: toggling starts/stops pumps, persists, and stays quiet for needs_key sources."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.config import Settings, sources
from heimdall.ingest.registry import build_connectors
from heimdall.ports import Connector, SourceControl
from heimdall.schemas import Domain, SourceHealth, SourceReliability
from heimdall.sources_ctl import SourceManager

HOSTS = ["testserver", "127.0.0.1", "localhost"]
CFG: dict[str, dict[str, Any]] = {
    "alpha": {"domain": "air", "reliability": "B", "label": "Alpha"},
    "beta": {"domain": "sea", "reliability": "C", "enabled": False},
}


class FakeConn:
    domain = Domain.AIR
    reliability = SourceReliability.B
    expected_latency_s = 1

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False
        self.started = asyncio.Event()

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        self.started.set()
        await asyncio.sleep(3600)
        yield []

    def health(self) -> SourceHealth:
        return SourceHealth(source=self.name, domain=self.domain, reliability=self.reliability)

    async def aclose(self) -> None:
        self.closed = True


def _manager(path: Path, built: list[FakeConn]) -> SourceManager:
    def build(key: str) -> Connector:
        c = FakeConn(key)
        built.append(c)
        return c

    async def pump(c: Connector) -> None:
        async for _ in c.stream():
            pass

    return SourceManager(CFG, build, pump, path)


async def test_toggle_persists_and_restarts_pump(tmp_path: Path) -> None:
    path = tmp_path / "state" / "sources.json"
    built: list[FakeConn] = []
    m = _manager(path, built)
    assert isinstance(m, SourceControl)
    await m.start()
    assert [c.name for c in built] == ["alpha"]  # beta is disabled in config
    await built[0].started.wait()
    states = {s.key: s for s in m.list()}
    assert states["alpha"].status == "ok" and states["alpha"].label == "Alpha"
    assert states["beta"].status == "off" and not states["beta"].enabled

    off = await m.set_enabled("alpha", False)
    assert off.status == "off" and built[0].closed and not m.connectors
    assert json.loads(path.read_text()) == {"alpha": False, "beta": False}

    on = await m.set_enabled("alpha", True)
    assert on.status == "ok" and len(built) == 2 and built[1].name == "alpha"
    await built[1].started.wait()
    await m.stop()
    assert built[1].closed

    # overrides win over sources.yaml on the next start
    built2: list[FakeConn] = []
    m2 = _manager(path, built2)
    await m2.start()
    assert [c.name for c in built2] == ["alpha"]  # persisted override: alpha on
    await m2.set_enabled("beta", True)
    assert [c.name for c in built2] == ["alpha", "beta"]
    await m2.stop()


async def test_unknown_key(tmp_path: Path) -> None:
    m = _manager(tmp_path / "s.json", [])
    with pytest.raises(KeyError):
        await m.set_enabled("nope", True)


def test_api_toggle_unknown_origin_and_key_format(tmp_path: Path) -> None:
    m = _manager(tmp_path / "s.json", [])
    app = create_app(MemoryStore(), playbook={"actions": {}}, allowed_hosts=HOSTS, sources=m)
    with TestClient(app) as c:
        assert {s["key"] for s in c.get("/api/sources").json()} == {"alpha", "beta"}
        r = c.post("/api/sources/beta", json={"enabled": True})
        assert r.status_code == 200 and r.json()["status"] == "ok"
        assert c.post("/api/sources/nope", json={"enabled": True}).status_code == 404
        assert c.post("/api/sources/BAD-key", json={"enabled": True}).status_code == 422
        bad = c.post(
            "/api/sources/beta", json={"enabled": False}, headers={"Origin": "http://evil.example"}
        )
        assert bad.status_code == 403
        assert c.get("/api/sources").json()[1]["enabled"] is True  # foreign POST changed nothing


@respx.mock
async def test_needs_key_toggle_makes_no_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AISSTREAM_KEY", raising=False)
    route = respx.route().mock(return_value=httpx.Response(200, json={}))
    cfg = sources()["sources"]

    def build(key: str) -> Connector | None:
        forced = {"sources": {key: {**cfg[key], "enabled": True}}}
        got = build_connectors(Settings(), forced, enabled={key})
        return got[0] if got else None

    async def pump(c: Connector) -> None:
        async for _ in c.stream():
            pass

    m = SourceManager({"aisstream": cfg["aisstream"]}, build, pump, tmp_path / "s.json")
    st = await m.set_enabled("aisstream", False)
    assert st.status == "off"
    st = await m.set_enabled("aisstream", True)
    await asyncio.sleep(0.05)
    assert st.status == "needs_key" and st.needs_key
    assert route.call_count == 0
    await m.stop()
