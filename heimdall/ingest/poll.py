"""Shared polling-connector base (tasks 017/018): health, needs_key, error hygiene, poll loop.

Errors are reported by type / HTTP status only, never `str(exc)`: an httpx error message holds the
request URL, and FIRMS carries its key in the URL path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel

from heimdall.ingest.adsb import SlidingCounter
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.logging import DROPPED, RECORDS_IN, get_logger
from heimdall.schemas import Domain, GeoEvent, SourceHealth, SourceReliability

log = get_logger("heimdall.poll")

Unit = Callable[[], Awaitable[tuple[list[GeoEvent], int]]]  # one request -> (events, n_dropped)


def parse_dt(v: Any) -> datetime | None:
    """ISO-8601 string (naive = UTC) -> aware UTC datetime, else None."""
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def num(v: Any) -> float | None:
    """Finite number or None (bool, str, NaN, inf rejected)."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if v == v and abs(v) != float("inf") else None


class PollConnector:
    """Subclasses set `domain` and implement `units()`; `token_name` set => keyed (ADR-004)."""

    domain: Domain
    token_name: str | None = None

    def __init__(
        self,
        name: str,
        *,
        token: str | None = None,
        reliability: SourceReliability = SourceReliability.B,
        expected_latency_s: int = 3600,
        poll_s: float = 900.0,
        rps: float = 1.0,
        client: RateLimitedClient | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.name = name
        self._token = token or None
        self.reliability = reliability
        self.expected_latency_s = expected_latency_s
        self.poll_s = poll_s
        self._rps = rps
        self._client = client
        self._sleep, self._now = sleep, now
        self._stop = False
        self.unauthorized = False
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.n_ok = self.n_err = self.n_dropped = 0
        self.records_hour = SlidingCounter()

    @property
    def needs_key(self) -> bool:
        return self.token_name is not None and not self._token

    @property
    def client(self) -> RateLimitedClient:
        if self._client is None:  # lazy: a keyless connector builds no HTTP client
            self._client = RateLimitedClient(self.name, rps=self._rps)
        return self._client

    def units(self) -> Sequence[Unit]:
        raise NotImplementedError

    def _fail(self, text: str) -> None:
        self.n_err += 1
        self.last_error = text[:200]
        self.last_error_at = self._now()
        log.warning("fetch_failed", source=self.name, error=self.last_error)

    def health(self) -> SourceHealth:
        total = self.n_ok + self.n_err
        h = SourceHealth(
            source=self.name,
            domain=self.domain,
            reliability=self.reliability,
            last_ok=self.last_ok,
            last_error=self.last_error,
            last_error_at=self.last_error_at,
            records_last_hour=self.records_hour.total(),
            error_rate=self.n_err / total if total else 0.0,
            expected_latency_s=self.expected_latency_s,
        )
        if self.needs_key:
            h.status, h.detail = "needs_key", f"set {self.token_name}"
        elif self.unauthorized:
            h.status, h.detail = "down", "unauthorized"
        elif self.last_error_at is not None and (
            self.last_ok is None or self.last_error_at >= self.last_ok
        ):
            h.status, h.detail = "degraded", self.last_error
        return h

    async def poll_once(self) -> list[GeoEvent]:
        if self.needs_key:
            return []
        out: dict[str, GeoEvent] = {}
        for unit in self.units():
            try:
                events, dropped = await unit()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    self.unauthorized = True
                    self._fail("unauthorized")
                    break  # every other request would fail the same way
                self._fail(f"HTTP {e.response.status_code}")
                continue
            except (httpx.HTTPError, CircuitOpenError, ValueError) as e:
                self._fail(type(e).__name__)
                continue
            self.unauthorized = False
            self.n_ok += 1
            self.last_ok = self._now()
            self.n_dropped += dropped
            if dropped:
                DROPPED.labels(self.name).inc(dropped)
            out.update((ev.id, ev) for ev in events)
        self.records_hour.add(len(out))
        RECORDS_IN.labels(self.name).inc(len(out))
        return list(out.values())

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        if self.needs_key:
            log.info("needs_key", source=self.name)
            return
        while not self._stop:
            batch = await self.poll_once()
            if batch:
                yield batch
            await self._sleep(self.poll_s)

    async def aclose(self) -> None:
        self._stop = True
        if self._client is not None:
            await self._client.aclose()
