"""Ctrl+C shutdown order: external polling stops first, the API port closes via uvicorn, bounded."""

from __future__ import annotations

import asyncio
from typing import Any

from heimdall.run import shutdown


class Log:
    def __init__(self) -> None:
        self.events: list[str] = []


class FakeManager:
    def __init__(self, log: Log) -> None:
        self.log = log
        self.connectors: list[Any] = []

    async def stop(self) -> None:
        self.log.events.append("connectors_stopped")


class FakeServer:
    """Mimics uvicorn: serve() returns once should_exit is set (after closing its socket)."""

    def __init__(self, log: Log, hang: bool = False) -> None:
        self.log, self.hang = log, hang
        self.should_exit = self.force_exit = False

    async def serve(self) -> None:
        while not self.should_exit or self.hang:
            await asyncio.sleep(0.01)
        self.log.events.append("server_closed_gracefully")


class Nop:
    def __init__(self, log: Log, name: str) -> None:
        self.log, self.name = log, name

    def flush(self) -> None:
        self.log.events.append(f"{self.name}_flush")

    async def close(self) -> None:
        self.log.events.append(f"{self.name}_close")

    def upsert_health(self, h: Any) -> None:
        pass


async def _run(hang: bool) -> tuple[Log, FakeServer, asyncio.Task[None]]:
    log = Log()
    srv = FakeServer(log, hang=hang)
    st = asyncio.create_task(srv.serve())
    bg = asyncio.create_task(asyncio.sleep(3600))
    store = Nop(log, "store")
    store.close = lambda: log.events.append("store_close")  # type: ignore[method-assign,assignment]
    await shutdown(
        FakeManager(log),
        srv,
        st,
        [bg],
        store,
        Nop(log, "writer"),
        Nop(log, "bus"),
        deadline_s=30,  # type: ignore[arg-type]
    )
    assert bg.cancelled()
    return log, srv, st


async def test_connectors_stop_before_api_and_server_closes_gracefully() -> None:
    log, srv, st = await _run(hang=False)
    assert log.events[:2] == ["connectors_stopped", "server_closed_gracefully"]
    assert srv.should_exit and not srv.force_exit and st.done() and not st.cancelled()
    assert log.events[-1] == "store_close"


async def test_hung_server_is_forced_not_left_serving() -> None:
    log, srv, st = await _run(hang=True)
    assert srv.force_exit and st.done()
    assert "server_closed_gracefully" not in log.events and log.events[-1] == "store_close"
