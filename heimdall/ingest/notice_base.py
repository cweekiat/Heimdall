"""Shared polling base for notice connectors (nga_msi, faa_nms).

Health follows. Only new/changed notices (content hash) are emitted per process.
Error detail strings are fixed vocabulary: they never echo response bodies or secrets.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from heimdall.config import _load_yaml
from heimdall.ingest.adsb import SlidingCounter
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.ingest.notice_parse import content_hash
from heimdall.logging import DROPPED, RECORDS_IN, get_logger
from heimdall.schemas import Domain, GeoEvent, SourceHealth, SourceReliability

log = get_logger("heimdall.notices")

Status = Literal["ok", "degraded", "down", "needs_key", "disabled"]
MAX_BACKOFF_S = 6 * 3600.0


class SourceMaintenance(Exception):
    """The source answered with something that is not the documented payload (e.g. HTML page)."""


class Unauthorized(Exception):
    pass


def load_warnings_cfg() -> dict[str, Any]:
    return _load_yaml("warnings.yaml")


class NoticeConnector:
    """Poll-based connector. Subclasses implement `_fetch() -> list[GeoEvent]`."""

    domain: Domain
    kind = "notice"

    def __init__(
        self,
        name: str,
        domain: Domain,
        reliability: SourceReliability,
        expected_latency_s: int,
        *,
        client: RateLimitedClient,
        poll_interval_s: float = 900.0,
        geo_blocked_codes: Sequence[int] = (),
        enabled: bool = True,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self.domain = domain
        self.reliability = reliability
        self.expected_latency_s = expected_latency_s
        self.client = client
        self.poll_interval_s = poll_interval_s
        self._sleep = sleep
        self._geo_codes = tuple(geo_blocked_codes)
        self._seen: dict[str, str] = {}
        self._stop = False
        self._consec_fail = 0
        self.status: Status = "ok" if enabled else "disabled"
        self.detail: str | None = None if enabled else "disabled in config"
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.n_ok = 0
        self.n_err = 0
        self.n_dropped = 0
        self.records_hour = SlidingCounter()

    # ---- to implement
    async def _fetch(self) -> list[GeoEvent]:
        raise NotImplementedError

    # ---- helpers
    def fresh(self, events: Sequence[GeoEvent]) -> list[GeoEvent]:
        """Only events whose content changed since this process last saw their id."""
        out: list[GeoEvent] = []
        for e in events:
            h = content_hash(e.text, e.title, e.valid_from, e.valid_to, e.attrs.get("status"))
            if self._seen.get(e.id) != h:
                self._seen[e.id] = h
                out.append(e)
        return out

    def _record_error(self, label: str, status: Status, detail: str) -> None:
        self.n_err += 1
        self._consec_fail += 1
        self.status, self.detail = status, detail
        self.last_error = label[:200]
        self.last_error_at = datetime.now(UTC)
        log.warning("fetch_failed", source=self.name, error=self.last_error)

    async def poll_once(self) -> list[GeoEvent]:
        """One poll; never raises for source errors. Returns only new/changed events."""
        if self.status in ("needs_key", "disabled"):
            return []
        try:
            events = await self._fetch()
        except SourceMaintenance:
            # a 200 with a wrong body resets the client's breaker; keep our own consecutive count
            self.client.breaker.failures = self._consec_fail
            self.client.breaker.failure()
            self._record_error("SourceMaintenance", "down", "source maintenance")
            return []
        except Unauthorized:
            self._record_error("Unauthorized", "down", "unauthorized")
            return []
        except CircuitOpenError:
            self._record_error("CircuitOpenError", "down", "circuit open")
            return []
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code in self._geo_codes:
                self._record_error(f"HTTP {code}", "down", "geo-blocked")
            elif code in (401, 403):
                self._record_error(f"HTTP {code}", "down", "unauthorized")
            else:
                self._record_error(f"HTTP {code}", "degraded", f"http {code}")
            return []
        except (httpx.HTTPError, ValueError) as e:
            self._record_error(type(e).__name__, "degraded", "fetch failed")
            return []
        self.n_ok += 1
        self._consec_fail = 0
        self.status, self.detail = "ok", None
        self.last_ok = datetime.now(UTC)
        new = self.fresh(events)
        self.records_hour.add(len(new))
        RECORDS_IN.labels(self.name).inc(len(new))
        return new

    def note_dropped(self, n: int) -> None:
        if n:
            self.n_dropped += n
            DROPPED.labels(self.name).inc(n)

    def health(self) -> SourceHealth:
        total = self.n_ok + self.n_err
        return SourceHealth(
            source=self.name,
            domain=self.domain,
            reliability=self.reliability,
            last_ok=self.last_ok,
            last_error=self.last_error,
            last_error_at=self.last_error_at,
            records_last_hour=self.records_hour.total(),
            error_rate=(self.n_err / total) if total else 0.0,
            expected_latency_s=self.expected_latency_s,
            status=self.status,
            detail=self.detail,
        )

    def next_delay(self) -> float:
        return min(MAX_BACKOFF_S, self.poll_interval_s * 2 ** min(self._consec_fail, 8))

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        while not self._stop and self.status not in ("needs_key", "disabled"):
            events = await self.poll_once()
            if events:
                yield events
            if self._stop or self.status in ("needs_key", "disabled"):
                break
            await self._sleep(self.next_delay())

    async def aclose(self) -> None:
        self._stop = True
        await self.client.aclose()
