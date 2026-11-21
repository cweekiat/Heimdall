"""AISStream (wss) connector + pure message parser.

Keyed: without AISSTREAM_KEY the connector makes no connection and reports needs_key.
The key is sent only inside the subscription frame; it is never logged nor put in health.detail.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import websockets
from pydantic import BaseModel, ValidationError

from heimdall.config import AOI, Settings, secret
from heimdall.geo import bbox_for_radius, h3_cell
from heimdall.ingest.adsb import SlidingCounter
from heimdall.logging import DROPPED, RECORDS_IN, get_logger
from heimdall.ports import Connector
from heimdall.schemas import Domain, SourceHealth, SourceReliability, VesselObs

log = get_logger("heimdall.ais")

PROVIDER = "aisstream"
POSITION_TYPES = ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport")
STATIC_TYPES = ("ShipStaticData", "StaticDataReport")
DEFAULT_MESSAGE_TYPES = [*POSITION_TYPES, *STATIC_TYPES]
STATIC_TTL_S = 24 * 3600.0
STATIC_MAX = 100_000

_TIME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(?:([+-]\d{4}|Z))?(?:\s+UTC)?$"
)


class StaticCache:
    """MMSI -> {name, ship_type, destination}; TTL-expiring bounded LRU."""

    def __init__(
        self,
        ttl_s: float = STATIC_TTL_S,
        max_size: int = STATIC_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s, self.max_size, self._clock = ttl_s, max_size, clock
        self._d: OrderedDict[int, tuple[float, dict[str, Any]]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._d)

    def update(self, mmsi: int, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return
        cur = self.get(mmsi) or {}
        cur.update(fields)
        self._d[mmsi] = (self._clock(), cur)
        self._d.move_to_end(mmsi)
        while len(self._d) > self.max_size:
            self._d.popitem(last=False)

    def get(self, mmsi: int) -> dict[str, Any] | None:
        item = self._d.get(mmsi)
        if item is None:
            return None
        stamped, fields = item
        if self._clock() - stamped > self.ttl_s:
            del self._d[mmsi]
            return None
        self._d.move_to_end(mmsi)
        return dict(fields)


def parse_time_utc(value: Any, fallback: datetime | None = None) -> datetime:
    """Parse AISStream `MetaData.time_utc` ("2022-12-29 18:22:32.318353123 +0000 UTC").

    Nanosecond fractions are truncated to microseconds. Falls back to `fallback` (receive time).
    """
    fb = fallback or datetime.now(UTC)
    if not isinstance(value, str):
        return fb
    m = _TIME_RE.match(value.strip())
    if not m:
        return fb
    date, hms, frac, _tz = m.groups()  # AISStream is always UTC
    micro = (frac or "0")[:6].ljust(6, "0")
    try:
        return datetime.fromisoformat(f"{date}T{hms}.{micro}+00:00")
    except ValueError:
        return fb


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _text(v: Any, limit: int = 40) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.replace("@", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit] or None


def _valid_mmsi(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int | str):
        return None
    s = str(v).strip()
    if len(s) == 9 and s.isdigit() and s[0] != "0":
        return int(s)
    return None


def _mmsi_of(msg: Mapping[str, Any], body: Mapping[str, Any]) -> int | None:
    meta = msg.get("MetaData")
    meta = meta if isinstance(meta, Mapping) else {}
    for cand in (body.get("UserID"), body.get("UserId"), meta.get("MMSI"), meta.get("MMSI_String")):
        if cand is not None:
            return _valid_mmsi(cand)
    return None


def _update_static(
    msg: Mapping[str, Any], mtype: str, body: Mapping[str, Any], mmsi: int, c: Any
) -> None:
    if mtype == "ShipStaticData":
        st = body.get("Type")
        c.update(
            mmsi,
            name=_text(body.get("Name")),
            ship_type=int(st) if isinstance(st, int) and st > 0 else None,
            destination=_text(body.get("Destination"), 20),
        )
    else:  # StaticDataReport: part A carries the name, part B the ship type
        ra, rb = body.get("ReportA"), body.get("ReportB")
        st = rb.get("ShipType") if isinstance(rb, Mapping) else None
        c.update(
            mmsi,
            name=_text(ra.get("Name")) if isinstance(ra, Mapping) else None,
            ship_type=int(st) if isinstance(st, int) and st > 0 else None,
        )


def parse_aisstream(
    msg: Mapping[str, Any], static: StaticCache, received: datetime | None = None
) -> VesselObs | None:
    """One AISStream message -> VesselObs, or None (static update, error, or implausible/dropped).

    Static messages only update `static`; later positions of the same MMSI pick the data up.
    """
    mtype = msg.get("MessageType")
    inner = msg.get("Message")
    if not isinstance(mtype, str) or not isinstance(inner, Mapping):
        return None
    body = inner.get(mtype)
    if not isinstance(body, Mapping):
        return None
    mmsi = _mmsi_of(msg, body)
    if mmsi is None:
        return None
    if mtype in STATIC_TYPES:
        _update_static(msg, mtype, body, mmsi, static)
        return None
    if mtype not in POSITION_TYPES:
        return None

    meta = msg.get("MetaData")
    meta = meta if isinstance(meta, Mapping) else {}
    lat = _num(body.get("Latitude"))
    lon = _num(body.get("Longitude"))
    if lat is None or lon is None:
        lat, lon = _num(meta.get("latitude")), _num(meta.get("longitude"))
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None  # includes 91 / 181 "not available"
    if lat == 0.0 and lon == 0.0:
        return None  # null island: default GNSS-less transmitter

    sog, cog, hdg = _num(body.get("Sog")), _num(body.get("Cog")), _num(body.get("TrueHeading"))
    nav = body.get("NavigationalStatus")
    info = static.get(mmsi) or {}
    name = info.get("name") or _text(meta.get("ShipName"))
    ship_type = info.get("ship_type")
    if mtype == "ExtendedClassBPositionReport":
        t = body.get("Type")
        if ship_type is None and isinstance(t, int) and t > 0:
            ship_type = t
        name = name or _text(body.get("Name"))
    try:
        return VesselObs(
            ts=parse_time_utc(meta.get("time_utc"), received),
            mmsi=mmsi,
            provider=PROVIDER,
            lat=lat,
            lon=lon,
            sog=sog if sog is not None and 0 <= sog < 102.3 else None,
            cog=cog if cog is not None and 0 <= cog < 360 else None,
            heading=hdg if hdg is not None and 0 <= hdg < 360 else None,
            name=name,
            ship_type=ship_type,
            nav_status=int(nav) if isinstance(nav, int) and 0 <= nav < 15 else None,
            destination=info.get("destination"),
            h3_r4=h3_cell(lat, lon, 4),
            h3_r6=h3_cell(lat, lon, 6),
        )
    except ValidationError:
        return None


def _default_connect(url: str) -> Any:
    return websockets.connect(url, open_timeout=10, max_size=2**20)


class AisStreamConnector:
    """One WebSocket for all AOI boxes (account limit: 3 connections)."""

    domain = Domain.SEA

    def __init__(
        self,
        name: str,
        url: str,
        aois: Sequence[AOI],
        api_key: str | None,
        *,
        message_types: Sequence[str] = DEFAULT_MESSAGE_TYPES,
        min_interval_s: float = 10.0,
        global_mode: bool = False,
        global_min_interval_s: float = 60.0,
        reliability: SourceReliability = SourceReliability.B,
        expected_latency_s: int = 5,
        connect: Callable[[str], Any] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = lambda: random.uniform(0.5, 1.0),
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 300.0,
        batch_max: int = 500,
        flush_s: float = 1.0,
    ) -> None:
        self.name = name
        self.url = url
        self.aois = list(aois)
        self._key = api_key or None
        self.message_types = list(message_types)
        self.min_interval_s = min_interval_s
        self.global_mode = global_mode  # 024g: world bbox, outside-AOI rows downsampled
        self.global_min_interval_s = global_min_interval_s
        self.reliability = reliability
        self.expected_latency_s = expected_latency_s
        self.connect = connect or _default_connect
        self._sleep, self._clock, self._jitter = sleep, clock, jitter
        self.backoff_base_s, self.backoff_cap_s = backoff_base_s, backoff_cap_s
        self.batch_max, self.flush_s = batch_max, flush_s
        self.static = StaticCache(clock=clock)
        self._last_emit: dict[int, float] = {}
        self._stop = False
        self.connected = False
        self._session_error = False  # server sent an error frame in this session
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.n_ok = 0
        self.n_err = 0
        self.n_dropped = 0
        self.n_thinned = 0
        self.records_hour = SlidingCounter(clock=clock)
        self.sleeps: list[float] = []  # backoff delays taken (observability/tests)

    # ------------------------------------------------------------------ helpers
    def _redact(self, s: str) -> str:
        return s.replace(self._key, "***") if self._key else s

    @property
    def set_aois(self) -> Callable[[Sequence[AOI]], None] | None:
        """In-place area update; only in global mode (the subscription box does not change)."""
        if not self.global_mode:
            return None

        def apply(aois: Sequence[AOI]) -> None:
            self.aois = list(aois)

        return apply

    def subscription(self) -> dict[str, Any]:
        boxes: list[list[list[float]]] = []
        if self.global_mode:
            boxes.append([[-90.0, -180.0], [90.0, 180.0]])
        for a in [] if self.global_mode else self.aois:
            for s, w, n, e in bbox_for_radius(a.lat, a.lon, a.radius_nm):
                boxes.append([[round(s, 4), round(w, 4)], [round(n, 4), round(e, 4)]])
        return {
            "APIKey": self._key,
            "BoundingBoxes": boxes,
            "FilterMessageTypes": self.message_types,
        }

    def _note_error(self, text: str) -> None:
        self.n_err += 1
        self.last_error = self._redact(text)[:200]
        self.last_error_at = datetime.now(UTC)
        log.warning("ais_error", source=self.name, error=self.last_error)

    def _delay(self, attempt: int) -> float:
        return min(self.backoff_cap_s, self.backoff_base_s * 2**attempt) * self._jitter()

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
        if not self._key:
            h.status, h.detail = "needs_key", "set AISSTREAM_KEY"
        elif self.last_error_at is not None and (
            self.last_ok is None or self.last_error_at >= self.last_ok
        ):
            h.status, h.detail = "degraded", self.last_error
        elif not self.connected:
            h.detail = "connecting"
        return h

    # ------------------------------------------------------------------ stream
    def _accept(self, msg: Mapping[str, Any], received: datetime) -> VesselObs | None:
        mtype = msg.get("MessageType")
        obs = parse_aisstream(msg, self.static, received)
        if obs is None:
            if mtype in POSITION_TYPES:
                self.n_dropped += 1
                DROPPED.labels(self.name).inc()
            return None
        interval = self.min_interval_s
        if self.global_mode and not any(a.contains(obs.lat, obs.lon) for a in self.aois):
            obs.is_global = True
            interval = self.global_min_interval_s
        now = obs.ts.timestamp()  # thin on observation time: deterministic, replay-safe
        last = self._last_emit.get(obs.mmsi)
        if last is not None and abs(now - last) < interval:
            self.n_thinned += 1
            return None
        self._last_emit[obs.mmsi] = now
        if len(self._last_emit) > 200_000:  # bound memory: forget entries past the interval
            cutoff = now - max(self.min_interval_s, self.global_min_interval_s)
            self._last_emit = {k: v for k, v in self._last_emit.items() if v > cutoff}
        return obs

    def _handle(self, raw: str | bytes, buf: list[VesselObs]) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            self.n_dropped += 1
            DROPPED.labels(self.name).inc()
            return
        if not isinstance(msg, dict):
            return
        if "error" in msg:
            self._note_error(f"server error: {msg['error']}")
            self._session_error = True
            return
        obs = self._accept(msg, datetime.now(UTC))
        if obs is not None:
            buf.append(obs)

    def _flush(self, buf: list[VesselObs]) -> list[VesselObs]:
        if buf:
            self.n_ok += 1
            self.last_ok = datetime.now(UTC)
            self.records_hour.add(len(buf))
            RECORDS_IN.labels(self.name).inc(len(buf))
        return buf

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        if not self._key:
            log.info("ais_needs_key", source=self.name)
            return
        attempt = 0
        while not self._stop:
            buf: list[VesselObs] = []
            self._session_error = False
            try:
                async with self.connect(self.url) as ws:
                    await ws.send(json.dumps(self.subscription()))  # within the 3 s window
                    self.connected = True
                    last_flush = self._clock()
                    while not self._stop:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.flush_s)
                        except TimeoutError:
                            raw = None
                        if raw is not None:
                            n_before = self.n_err
                            self._handle(raw, buf)
                            if self.n_err == n_before:
                                attempt = 0  # healthy traffic resets the backoff
                        if buf and (
                            len(buf) >= self.batch_max or self._clock() - last_flush >= self.flush_s
                        ):
                            out, buf = buf, []
                            last_flush = self._clock()
                            yield self._flush(out)
            except (websockets.WebSocketException, OSError, TimeoutError) as e:
                if not self._session_error:  # keep the server's reason as the visible error
                    self._note_error(f"{type(e).__name__}: {e}")
            finally:
                self.connected = False
            if buf:
                yield self._flush(buf)
            if self._stop:
                break
            delay = self._delay(attempt)
            attempt += 1
            self.sleeps.append(delay)
            await self._sleep(delay)

    async def aclose(self) -> None:
        self._stop = True


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    env = str(cfg.get("auth", "AISSTREAM_KEY"))
    return AisStreamConnector(
        key,
        str(cfg["base"]),
        settings.aois,
        secret(env),
        message_types=list(cfg.get("message_types", DEFAULT_MESSAGE_TYPES)),
        min_interval_s=float(cfg.get("min_interval_s", 10)),
        global_mode=bool(cfg.get("global", False)),
        global_min_interval_s=float(cfg.get("global_min_interval_s", 60)),
        reliability=SourceReliability(cfg.get("reliability", "B")),
        expected_latency_s=int(cfg.get("latency_s", 5)),
    )
