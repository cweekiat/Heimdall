"""Runtime source manager (task 012a): one pump task per connector, on/off at runtime.

Implements `heimdall.ports.SourceControl`. Overrides persist in a small JSON file
(`data/state/sources.json`), are read at startup and win over `enabled` in `sources.yaml`.
A needs_key source may be switched on; its connector then makes no requests (ADR-004).
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import json
import os
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any

from heimdall.logging import get_logger
from heimdall.ports import Connector
from heimdall.schemas import Domain, SourceReliability, SourceState, keys_for

log = get_logger("heimdall.sources_ctl")

Builder = Callable[[str], Connector | None]
Pump = Callable[[Connector], Coroutine[Any, Any, None]]


class SourceManager:
    def __init__(
        self,
        cfg: Mapping[str, Mapping[str, Any]],
        build: Builder,
        pump: Pump,
        state_path: Path | None = None,
    ) -> None:
        self._cfg = cfg
        self._build = build
        self._pump = pump
        self._path = state_path
        self._enabled: dict[str, bool] = {
            k: dict(c).get("enabled", True) is not False for k, c in cfg.items()
        }
        for k, v in self._load().items():
            if k in self._enabled:
                self._enabled[k] = v
        self._conns: dict[str, Connector] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._orphans: builtins.list[Connector] = []

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict[str, bool]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError) as e:
            log.warning("sources_state_unreadable", error=str(e))
            return {}
        if not isinstance(raw, dict):
            log.warning("sources_state_malformed", type=type(raw).__name__)
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, bool)}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self._enabled, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    # ------------------------------------------------------------ lifecycle
    @property
    def connectors(self) -> builtins.list[Connector]:
        return list(self._conns.values())

    @property
    def tasks(self) -> builtins.list[asyncio.Task[None]]:
        return list(self._tasks.values())

    def _start(self, key: str) -> None:
        task = self._tasks.get(key)
        if task is not None:
            if not task.done():
                return
            log.warning("source_pump_dead_restart", key=key)
            self._tasks.pop(key, None)
            old = self._conns.pop(key, None)
            if old is not None:
                self._orphans.append(old)
        conn = self._build(key)
        if conn is None:
            return
        self._conns[key] = conn
        self._tasks[key] = asyncio.create_task(self._pump(conn))

    async def _stop(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        conn = self._conns.pop(key, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if conn is not None:
            try:
                await conn.aclose()
            except Exception as e:
                log.error("source_aclose_failed", key=key, error=str(e))

    async def _drain_orphans(self) -> None:
        orphans, self._orphans = self._orphans, []
        for conn in orphans:
            try:
                await conn.aclose()
            except Exception as e:
                log.error("source_orphan_aclose_failed", error=str(e))

    async def start(self) -> None:
        for key, on in self._enabled.items():
            if on:
                self._start(key)

    async def stop(self) -> None:
        # all at once: stopping one by one kept the others polling external APIs meanwhile
        await asyncio.gather(*(self._stop(k) for k in list(self._tasks)), return_exceptions=True)
        await self._drain_orphans()

    # ------------------------------------------------------------ SourceControl
    def _state(self, key: str) -> SourceState:
        cfg = self._cfg[key]
        on = self._enabled[key]
        domain = Domain(cfg.get("domain", "events"))
        rel = SourceReliability(cfg.get("reliability", "F"))
        conn = self._conns.get(key)
        label = str(cfg.get("label", key))
        if not on:
            return SourceState(
                key=key, label=label, domain=domain, enabled=on, status="off", reliability=rel
            )
        if conn is None:
            return SourceState(
                key=key,
                label=label,
                domain=domain,
                enabled=on,
                status="disabled",
                detail="not installed",
                reliability=rel,
            )
        task = self._tasks.get(key)
        h = conn.health()
        # a pump that ended because the source needs a key (or was refused access) is not a crash
        if task is not None and task.done() and h.status != "needs_key":
            return SourceState(
                key=key,
                label=label,
                domain=domain,
                enabled=on,
                status="down",
                detail="pump task stopped; toggle to restart",
                reliability=rel,
            )
        return SourceState(
            key=key,
            label=label,
            domain=domain,
            enabled=on,
            status=h.status,
            detail=h.detail,
            last_ok=h.last_ok,
            records_last_hour=h.records_last_hour,
            error_rate=h.error_rate,
            reliability=h.reliability,
            needs_key=h.status == "needs_key",
        )

    def list(self) -> builtins.list[SourceState]:
        out = []
        for k in self._cfg:
            st = self._state(k)
            st.about = str(self._cfg[k].get("about") or "") or None
            st.keys = keys_for(k)
            out.append(st)
        return out

    def _dead(self, key: str) -> bool:
        t = self._tasks.get(key)
        return t is None or t.done()

    async def set_enabled(self, key: str, enabled: bool) -> SourceState:
        if key not in self._cfg:
            raise KeyError(key)
        if self._enabled[key] != enabled or (enabled and self._dead(key)):
            self._enabled[key] = enabled
            if enabled:
                self._start(key)
                await self._drain_orphans()
            else:
                await self._stop(key)
            self._save()
        return self._state(key)

    def update_aois(self, key: str, aois: builtins.list[Any]) -> bool:
        """Give a running connector a new area list in place; False when it cannot take one."""
        fn = getattr(self._conns.get(key), "set_aois", None)
        if fn is None:
            return False
        fn(aois)
        return True

    async def restart(self, key: str) -> None:
        if key not in self._cfg:
            raise KeyError(key)
        if not self._enabled[key]:
            return
        await self._stop(key)
        self._start(key)
        await self._drain_orphans()
