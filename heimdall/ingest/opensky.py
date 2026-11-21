"""OpenSky global aircraft states: display-only worldwide traffic.

Keyed: needs an OpenSky API client (OAuth2 client credentials, stored in the key store as
OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET). Without them the connector makes no request and reports
needs_key. Credentials are sent only to the token endpoint and never logged.

Rows are marked provider=opensky, is_global=True; AOI detectors skip them (no NIC/NACp). Positions
inside an AOI are dropped here: the ADS-B aggregators already cover those with better quality.
Pacing follows the X-Rate-Limit-Remaining header (see `next_delay`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from heimdall.config import AOI, Settings, secret
from heimdall.geo import h3_cell
from heimdall.ingest.adsb import SlidingCounter
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.logging import DROPPED, RECORDS_IN, get_logger
from heimdall.ports import Connector
from heimdall.schemas import AircraftObs, Domain, SourceHealth, SourceReliability

log = get_logger("heimdall.opensky")

PROVIDER = "opensky"
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)
GLOBAL_COST = 4  # credits per bbox-less /states/all call
MIN_DELAY_S = 10.0  # data resolution
MAX_DELAY_S = 300.0
M_TO_FT, MS_TO_KT, MS_TO_FPM = 3.28084, 1.943844, 196.850394
_SRC = {0: "opensky_adsb", 2: "opensky_mlat", 3: "opensky_flarm"}


def next_delay(
    remaining: int | None,
    reset_in_s: float,
    *,
    poll_s: float = 90.0,
    cost: int = GLOBAL_COST,
    min_s: float = MIN_DELAY_S,
    max_s: float = MAX_DELAY_S,
) -> float:
    """Seconds to wait before the next global call: spread remaining credits until the reset."""
    if remaining is None:
        d = poll_s
    else:
        calls = remaining // cost
        d = max_s if calls <= 0 else max(reset_in_s, 0.0) / calls
    return min(max_s, max(min_s, d))


def seconds_to_utc_reset(now: datetime) -> float:
    nxt = (now.astimezone(UTC) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (nxt - now).total_seconds()


def _f(v: Any) -> float | None:
    return None if isinstance(v, bool) or not isinstance(v, int | float) else float(v)


def parse_states(
    payload: Mapping[str, Any], fetched_at: datetime | None = None
) -> tuple[list[AircraftObs], int]:
    """OpenSky /states/all -> (rows, dropped). Nulls tolerated; implausible rows dropped."""
    fb = fetched_at or datetime.now(UTC)
    states = payload.get("states")
    if not isinstance(states, list):
        return [], 0
    out: list[AircraftObs] = []
    dropped = 0
    for st in states:
        if not isinstance(st, list) or len(st) < 17:
            dropped += 1
            continue
        hex_ = st[0].strip().lower() if isinstance(st[0], str) else ""
        lon, lat = _f(st[5]), _f(st[6])
        if not hex_ or lat is None or lon is None:
            dropped += 1  # no position (OpenSky lists such states too)
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0.0 and lon == 0.0):
            dropped += 1
            continue
        t = _f(st[3]) if st[3] is not None else _f(st[4])
        ts = datetime.fromtimestamp(t, UTC) if t is not None else fb
        alt, geo, vel = _f(st[7]), _f(st[13]), _f(st[9])
        if (alt is not None and not -500 <= alt <= 25000) or (
            vel is not None and not 0 <= vel < 700
        ):
            dropped += 1  # physically implausible (> 82 000 ft or > Mach 2)
            continue
        trk, vr = _f(st[10]), _f(st[11])
        flight = st[1].strip() if isinstance(st[1], str) else ""
        try:
            out.append(
                AircraftObs(
                    ts=ts,
                    hex=hex_,
                    provider=PROVIDER,
                    lat=lat,
                    lon=lon,
                    src_type=_SRC.get(st[16] if isinstance(st[16], int) else -1, "opensky_other"),
                    flight=flight or None,
                    on_ground=bool(st[8]),
                    alt_baro=None if alt is None or st[8] else round(alt * M_TO_FT),
                    alt_geom=None if geo is None else round(geo * M_TO_FT),
                    gs=None if vel is None else round(vel * MS_TO_KT, 1),
                    track=trk if trk is not None and 0 <= trk < 360 else None,
                    baro_rate=None if vr is None else round(vr * MS_TO_FPM),
                    h3_r4=h3_cell(lat, lon, 4),
                    h3_r6=h3_cell(lat, lon, 6),
                    is_global=True,
                )
            )
        except ValidationError:
            dropped += 1
    return out, dropped


class OpenSkyConnector:
    domain = Domain.AIR

    def __init__(
        self,
        name: str,
        base_url: str,
        aois: Sequence[AOI],
        client_id: str | None,
        client_secret: str | None,
        *,
        poll_s: float = 90.0,
        reliability: SourceReliability = SourceReliability.B,
        expected_latency_s: int = 10,
        client: RateLimitedClient | None = None,
        token_url: str = TOKEN_URL,
        token_transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.aois = aois  # live reference: user-drawn areas apply immediately
        self._id, self._secret = client_id or None, client_secret or None
        self.poll_s = poll_s
        self.reliability = reliability
        self.expected_latency_s = expected_latency_s
        # pacing is ours (credit budget); the client keeps breaker + adaptive rate, 429 is ours
        self.client = client or RateLimitedClient(name, rps=1.0, max_retries=0)
        self.token_url = token_url
        self._token_client = httpx.AsyncClient(timeout=20.0, transport=token_transport)
        self._sleep, self._clock, self._now = sleep, clock, now
        self._token: str | None = None
        self._token_exp = 0.0
        self._stop = False
        self.credits_left: int | None = None
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.n_ok = self.n_err = self.n_dropped = self.n_in_aoi = 0
        self.records_hour = SlidingCounter(clock=clock)
        self.delays: list[float] = []  # pacing delays taken (observability/tests)

    def _redact(self, s: str) -> str:
        for v in (self._id, self._secret, self._token):
            if v:
                s = s.replace(v, "***")
        return s

    def _note_error(self, text: str) -> None:
        self.n_err += 1
        self.last_error = self._redact(text)[:200]
        self.last_error_at = datetime.now(UTC)
        log.warning("opensky_error", source=self.name, error=self.last_error)

    async def _bearer(self) -> str:
        if self._token and self._clock() < self._token_exp:
            return self._token
        resp = await self._token_client.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._id,
                "client_secret": self._secret,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        tok = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(tok, str) or not tok:
            raise ValueError("token response without access_token")
        ttl = _f(body.get("expires_in")) or 300.0
        self._token, self._token_exp = tok, self._clock() + max(ttl - 30.0, 1.0)
        return tok

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
        if not (self._id and self._secret):
            h.status, h.detail = "needs_key", "set OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET"
        elif self.last_error_at is not None and (
            self.last_ok is None or self.last_error_at >= self.last_ok
        ):
            h.status, h.detail = "degraded", self.last_error
        elif self.credits_left is not None:
            h.detail = f"{self.credits_left} credits left"
        return h

    async def poll_once(self) -> tuple[list[AircraftObs], float]:
        """One global call; returns (rows, seconds to wait). Never raises for source errors."""
        wait = self.poll_s
        try:
            tok = await self._bearer()
            resp = await self.client.get(
                f"{self.base_url}/states/all", headers={"Authorization": f"Bearer {tok}"}
            )
            payload = resp.json()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            self._note_error(f"HTTP {code}")
            if code == 401:
                self._token = None
            if code == 429:
                ra = _f(_num_header(e.response, "X-Rate-Limit-Retry-After-Seconds"))
                return [], max(MIN_DELAY_S, ra if ra is not None else MAX_DELAY_S)
            return [], MAX_DELAY_S
        except (httpx.HTTPError, CircuitOpenError, ValueError) as e:
            self._note_error(f"{type(e).__name__}: {e}")
            return [], MAX_DELAY_S
        rem = _num_header(resp, "X-Rate-Limit-Remaining")
        if rem is not None:
            self.credits_left = int(rem)
            wait = next_delay(
                self.credits_left, seconds_to_utc_reset(self._now()), poll_s=self.poll_s
            )
        rows, dropped = parse_states(payload if isinstance(payload, dict) else {}, self._now())
        kept = [r for r in rows if not any(a.contains(r.lat, r.lon) for a in self.aois)]
        self.n_in_aoi += len(rows) - len(kept)
        self.n_ok += 1
        self.last_ok = datetime.now(UTC)
        self.n_dropped += dropped
        self.records_hour.add(len(kept))
        RECORDS_IN.labels(self.name).inc(len(kept))
        if dropped:
            DROPPED.labels(self.name).inc(dropped)
        return kept, wait

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        if not (self._id and self._secret):
            log.info("opensky_needs_key", source=self.name)
            return
        while not self._stop:
            rows, wait = await self.poll_once()
            if rows:
                yield rows
            self.delays.append(wait)
            await self._sleep(wait)

    async def aclose(self) -> None:
        self._stop = True
        await self.client.aclose()
        await self._token_client.aclose()


def _num_header(resp: httpx.Response, name: str) -> float | None:
    try:
        return float(resp.headers[name])
    except (KeyError, ValueError):
        return None


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return OpenSkyConnector(
        key,
        str(cfg["base"]),
        settings.aois,
        secret("OPENSKY_CLIENT_ID"),
        secret("OPENSKY_CLIENT_SECRET"),
        poll_s=float(cfg.get("poll_s", 90)),
        reliability=SourceReliability(cfg.get("reliability", "B")),
        expected_latency_s=int(cfg.get("latency_s", 10)),
    )
