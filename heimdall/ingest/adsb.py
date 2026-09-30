"""ADS-B aggregator connectors (readsb v2 API) + cross-provider de-duplication."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel

from heimdall.config import AOI
from heimdall.geo import haversine_km
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.ingest.readsb import parse_readsb_v2
from heimdall.logging import DROPPED, RECORDS_IN, get_logger
from heimdall.schemas import AircraftObs, Domain, SourceHealth, SourceReliability

log = get_logger("heimdall.adsb")

MAX_RADIUS_NM = 250.0  # aggregator API cap


class SlidingCounter:
    """Count of events over the trailing `window_s` seconds."""

    def __init__(self, window_s: float = 3600.0, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._q: deque[tuple[float, int]] = deque()
        self._total = 0

    def add(self, n: int) -> None:
        if n:
            self._q.append((self._clock(), n))
            self._total += n

    def total(self) -> int:
        cutoff = self._clock() - self.window_s
        while self._q and self._q[0][0] <= cutoff:
            self._total -= self._q.popleft()[1]
        return self._total


class AdsbAggregatorConnector:
    """One connector per provider (adsb_lol / adsb_fi), own rate limit.

    `path_template` uses {lat}, {lon}, {radius}. Round-robins the configured AOIs.
    """

    domain = Domain.AIR

    def __init__(
        self,
        name: str,
        base_url: str,
        path_template: str,
        aois: Sequence[AOI],
        *,
        provider: str | None = None,
        rps: float = 1.0,
        reliability: SourceReliability = SourceReliability.B,
        expected_latency_s: int = 5,
        client: RateLimitedClient | None = None,
        idle_sleep_s: float = 5.0,
    ) -> None:
        self.name = name
        self.provider = provider or name.replace("_", ".")
        self.reliability = reliability
        self.expected_latency_s = expected_latency_s
        self.base_url = base_url.rstrip("/")
        self.path_template = path_template
        self.aois = list(aois)
        self.client = client or RateLimitedClient(name, rps=rps)
        self.idle_sleep_s = idle_sleep_s
        self._i = 0
        self._stop = False
        # health bookkeeping (read by heimdall.run)
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.n_ok = 0
        self.n_err = 0
        self.n_records = 0
        self.records_hour = SlidingCounter()
        self.n_dropped = 0
        self.denied: str | None = None  # set on HTTP 401/403: stop polling until restarted

    def url_for(self, aoi: AOI) -> str:
        radius = min(aoi.radius_nm, MAX_RADIUS_NM)
        path = self.path_template.format(lat=aoi.lat, lon=aoi.lon, radius=int(radius))
        return f"{self.base_url}{path}"

    async def fetch_aoi(self, aoi: AOI) -> tuple[list[AircraftObs], int]:
        resp = await self.client.get(self.url_for(aoi))
        fetched_at = datetime.now(UTC)
        payload: dict[str, Any] = resp.json()
        if isinstance(payload.get("now"), int | float):
            now = float(payload["now"])
            if now > 1e11:  # some providers report ms
                now /= 1000.0
            fetched_at = datetime.fromtimestamp(now, UTC)
        return parse_readsb_v2(payload, self.provider, fetched_at)

    async def poll_once(self) -> list[AircraftObs]:
        """Fetch the next AOI in the round-robin; never raises for source errors."""
        if not self.aois:
            return []
        aoi = self.aois[self._i % len(self.aois)]
        self._i += 1
        try:
            obs, dropped = await self.fetch_aoi(aoi)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                self.denied = (
                    f"access denied (HTTP {e.response.status_code}): provider needs approval "
                    "or a key; polling stopped"
                )
                log.warning("access_denied", source=self.name, status=e.response.status_code)
            return self._failed(aoi, e)
        except (httpx.HTTPError, CircuitOpenError, ValueError) as e:
            return self._failed(aoi, e)
        self.n_ok += 1

        self.last_ok = datetime.now(UTC)
        self.n_records += len(obs)
        self.records_hour.add(len(obs))
        self.n_dropped += dropped
        RECORDS_IN.labels(self.name).inc(len(obs))
        if dropped:
            DROPPED.labels(self.name).inc(dropped)
        return obs

    def _failed(self, aoi: AOI, e: Exception) -> list[AircraftObs]:
        self.n_err += 1
        self.last_error = f"{type(e).__name__}: {e}"[:200]
        self.last_error_at = datetime.now(UTC)
        log.warning("fetch_failed", source=self.name, aoi=aoi.name, error=self.last_error)
        return []

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
        if self.last_error_at is not None and (
            self.last_ok is None or self.last_error_at >= self.last_ok
        ):
            h.status, h.detail = "degraded", self.last_error
        if self.denied:
            h.status, h.detail = "needs_key", self.denied
        return h

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        while not self._stop and not self.denied:
            n_err = self.n_err
            obs = await self.poll_once()
            if obs:
                yield obs
            elif self.n_err > n_err:
                # back off only on errors; an empty AOI goes straight to the next one
                # (the client's rate limiter already paces requests at `rps`)
                await asyncio.sleep(self.idle_sleep_s)

    async def aclose(self) -> None:
        self._stop = True
        await self.client.aclose()


def merge_providers(
    obs: Sequence[AircraftObs], max_dt_s: float = 2.0, max_km: float = 0.5
) -> tuple[list[AircraftObs], dict[str, set[str]]]:
    """De-duplicate the same aircraft reported by several providers.

    Same hex and |dt| < max_dt_s and distance < max_km -> one kept (first by ts).
    Returns (kept, providers_by_hex); the side-dict feeds Coverage.providers later.
    """
    by_hex: dict[str, list[AircraftObs]] = defaultdict(list)
    providers: dict[str, set[str]] = defaultdict(set)
    for o in obs:
        by_hex[o.hex].append(o)
        providers[o.hex].add(o.provider)
    kept: list[AircraftObs] = []
    for rows in by_hex.values():
        rows.sort(key=lambda r: r.ts)
        group: list[AircraftObs] = []
        for r in rows:
            dup = any(
                abs((r.ts - k.ts).total_seconds()) < max_dt_s
                and haversine_km(r.lat, r.lon, k.lat, k.lon) < max_km
                for k in group
            )
            if not dup:
                group.append(r)
        kept.extend(group)
    kept.sort(key=lambda r: r.ts)
    return kept, dict(providers)
