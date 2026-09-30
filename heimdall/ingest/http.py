"""Polite async HTTP client: token bucket, backoff with jitter, circuit breaker."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from heimdall.logging import CIRCUIT_OPEN, HTTP_ERRORS, get_logger

USER_AGENT = "heimdall-osint/0.1"
log = get_logger("heimdall.http")


class CircuitOpenError(RuntimeError):
    pass


class TokenBucket:
    def __init__(
        self,
        rps: float,
        burst: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rps <= 0:
            raise ValueError(f"rps must be > 0, got {rps}")
        self.rate = rps
        self.capacity = max(burst, 1.0)
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await self._sleep((1.0 - self.tokens) / self.rate)


class CircuitBreaker:
    """Opens after `threshold` consecutive failures; half-open probe after `reset_s`."""

    def __init__(
        self,
        threshold: int = 5,
        reset_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.reset_s = reset_s
        self._clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self.opened_at >= self.reset_s else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def open_for(self, seconds: float) -> None:
        """Force the circuit open until `seconds` from now (e.g. a long Retry-After)."""
        self.opened_at = self._clock() + seconds - self.reset_s

    def failure(self) -> bool:
        """Record a failure; returns True if this call (re)opened the circuit."""
        self.failures += 1
        if self.failures >= self.threshold or self.opened_at is not None:
            newly = self.opened_at is None
            self.opened_at = self._clock()
            return newly
        return False


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("Retry-After")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        try:
            dt = parsedate_to_datetime(v)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError):
            return None


MIN_RPS = 0.05  # floor for adaptive slow-down after HTTP 429 (one request per 20 s)


class RateLimitedClient:
    def __init__(
        self,
        source: str,
        rps: float = 1.0,
        *,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self.bucket = TokenBucket(rps, clock=clock, sleep=sleep)
        self.max_rps = rps
        self.min_rps = min(rps, MIN_RPS)
        self.breaker = CircuitBreaker(clock=clock)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=timeout, transport=transport
        )

    def _delay(self, attempt: int, resp: httpx.Response | None) -> float:
        ra = _retry_after(resp) if resp is not None else None
        if ra is not None:
            return ra
        exp = min(self.backoff_cap, self.backoff_base * 2**attempt)
        return random.uniform(0, exp)  # full jitter

    def _fail(self) -> None:
        HTTP_ERRORS.labels(self.source).inc()
        if self.breaker.failure():
            CIRCUIT_OPEN.labels(self.source).inc()
            log.warning("circuit_open", source=self.source)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        if not self.breaker.allow():
            raise CircuitOpenError(f"{self.source}: circuit open")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self.bucket.acquire()
            resp: httpx.Response | None = None
            try:
                resp = await self._client.get(url, **kwargs)
            except httpx.TransportError as e:
                last_exc = e
            else:
                if resp.status_code == 429:
                    # adaptive: the server says we are too fast, so halve our own rate
                    self.bucket.rate = max(self.min_rps, self.bucket.rate / 2)
                    log.info("rate_reduced", source=self.source, rps=round(self.bucket.rate, 3))
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp
                    )
                else:
                    self.breaker.success()
                    # recover slowly (+5% per success) back towards the configured rate
                    self.bucket.rate = min(self.max_rps, self.bucket.rate * 1.05)
                    resp.raise_for_status()  # other 4xx: not retryable
                    return resp
            self._fail()
            ra = _retry_after(resp) if resp is not None else None
            if ra is not None and ra > self.backoff_cap:
                # Server asked for a long pause: stop retrying, keep the circuit open that long.
                self.breaker.open_for(ra)
                log.warning("retry_after_exceeds_cap", source=self.source, retry_after=ra)
                break
            if attempt == self.max_retries or not self.breaker.allow():
                break
            await self._sleep(self._delay(attempt, resp))
        assert last_exc is not None
        raise last_exc

    async def aclose(self) -> None:
        await self._client.aclose()
