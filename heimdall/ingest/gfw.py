"""Global Fishing Watch (API v3) events connector + pure parser (ADR-005 section A).

Keyed (ADR-004): without GFW_TOKEN the connector makes no request and reports needs_key.
The token is sent only in the Authorization header; it never reaches logs or health.detail.
NOTE: the events query shape (GET + `geometry`) and the SAR dataset are unverified against the
live API (task 005 re-check); everything is config-driven so a fix does not touch the parser.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from heimdall.config import AOI, Settings
from heimdall.geo import bbox_for_radius, h3_cell, within_radius_nm
from heimdall.ingest.adsb import SlidingCounter
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.logging import DROPPED, RECORDS_IN, get_logger
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceHealth, SourceReliability

log = get_logger("heimdall.gfw")

# config dataset key -> (GFW dataset id, GeoEvent.kind, domain)
DATASETS: dict[str, tuple[str, str, Domain]] = {
    "gaps": ("public-global-gaps-events:latest", "gfw_gap", Domain.SEA),
    "loitering": ("public-global-loitering-events:latest", "gfw_loitering", Domain.SEA),
    "encounters": ("public-global-encounters-events:latest", "gfw_encounter", Domain.SEA),
    "sar": ("public-global-sar-presence:latest", "sar_detection", Domain.SPACE),  # unverified
}
_ALIASES = {"gap": "gaps", "encounter": "encounters"}
PAGE_LIMIT = 100
MAX_PAGES = 50


def _dt(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def _mmsi(v: Any) -> int | None:
    s = str(v).strip() if isinstance(v, int | str) and not isinstance(v, bool) else ""
    return int(s) if len(s) == 9 and s.isdigit() and s[0] != "0" else None


def _float(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _short(v: Any, n: int = 80) -> str | None:
    return " ".join(v.split())[:n] or None if isinstance(v, str) else None


def _entry_to_event(e: Mapping[str, Any], kind: str, domain: Domain, detail_key: str) -> GeoEvent:
    start, end = _dt(e.get("start")), _dt(e.get("end"))
    if start is None:
        raise ValueError("no start")
    pos = e.get("position")
    pos = pos if isinstance(pos, Mapping) else {}
    lat, lon = _float(pos.get("lat")), _float(pos.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise ValueError("no plausible position")
    if end is not None and end < start:
        raise ValueError("end before start")
    eid = e.get("id")
    if not isinstance(eid, str) or not eid:
        raise ValueError("no id")
    det = e.get(detail_key)
    det = det if isinstance(det, Mapping) else {}
    hours = _float(det.get("durationHours")) or _float(det.get("totalTimeHours"))
    if hours is None and end is not None:
        hours = (end - start).total_seconds() / 3600.0
    vessel = e.get("vessel")
    vessel = vessel if isinstance(vessel, Mapping) else {}
    attrs: dict[str, Any] = {
        "mmsi": _mmsi(vessel.get("ssvid")),
        "gfw_vessel_id": _short(vessel.get("id"), 120),
    }
    if (flag := _short(vessel.get("flag"), 3)) is not None:
        attrs["flag"] = flag
    if kind == "gfw_encounter":
        other = det.get("vessel")
        attrs["partner_mmsi"] = _mmsi(other.get("ssvid")) if isinstance(other, Mapping) else None
    if kind == "gfw_gap":
        attrs["intentional_disabling"] = bool(det.get("intentionalDisabling", False))
    return GeoEvent(
        id=f"gfw:{eid}",
        ts=start,
        source="gfw",
        domain=domain,
        kind=kind,
        lat=lat,
        lon=lon,
        valid_from=start,
        valid_to=end,
        title=kind.replace("_", " "),
        text=_short(vessel.get("name")),  # untrusted
        value=hours,
        attrs=attrs,
        h3_r4=h3_cell(lat, lon, 4),
        h3_r6=h3_cell(lat, lon, 6),
    )


def parse_gfw_events_counted(payload: Mapping[str, Any], kind: str) -> tuple[list[GeoEvent], int]:
    """(events, n_dropped) for one page. `kind` is a dataset key: gaps|loitering|encounters|sar."""
    key = _ALIASES.get(kind, kind)
    if key not in DATASETS:
        raise ValueError(f"unknown GFW kind {kind!r}")
    _, ev_kind, domain = DATASETS[key]
    detail_key = {"gaps": "gap", "loitering": "loitering", "encounters": "encounter"}.get(key, key)
    entries = payload.get("entries")
    out: list[GeoEvent] = []
    dropped = 0
    for e in entries if isinstance(entries, list) else []:
        try:
            out.append(_entry_to_event(e, ev_kind, domain, detail_key))
        except (ValueError, TypeError, ValidationError, AttributeError):
            dropped += 1
    return out, dropped


def parse_gfw_events(payload: Mapping[str, Any], kind: str) -> list[GeoEvent]:
    return parse_gfw_events_counted(payload, kind)[0]


class GfwConnector:
    domain = Domain.SEA

    def __init__(
        self,
        name: str,
        base_url: str,
        aois: Sequence[AOI],
        token: str | None,
        *,
        datasets: Sequence[str] = ("gaps", "loitering", "encounters"),
        sar: bool = False,
        lookback_days: int = 3,
        poll_s: float = 21600.0,
        rps: float = 1.0,
        reliability: SourceReliability = SourceReliability.B,
        expected_latency_s: int = 86400,
        client: RateLimitedClient | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.aois = list(aois)
        self._token = token or None
        keys = [_ALIASES.get(d, d) for d in datasets]
        self.datasets = [d for d in keys if d in DATASETS and (d != "sar" or sar)]
        if sar and "sar" not in self.datasets:
            self.datasets.append("sar")
        self.lookback_days, self.poll_s = lookback_days, poll_s
        self.reliability = reliability
        self.expected_latency_s = expected_latency_s
        self._client = client
        self._rps = rps
        self._sleep, self._now = sleep, now
        self._stop = False
        self.unauthorized = False
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.n_ok = 0
        self.n_err = 0
        self.n_dropped = 0
        self.records_hour = SlidingCounter()

    @property
    def client(self) -> RateLimitedClient:
        if self._client is None:  # created lazily so a keyless connector builds no HTTP client
            self._client = RateLimitedClient(self.name, rps=self._rps)
        return self._client

    def _fail(self, text: str) -> None:
        self.n_err += 1
        self.last_error = text[:200]
        self.last_error_at = self._now()
        log.warning("gfw_fetch_failed", source=self.name, error=self.last_error)

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
        if not self._token:
            h.status, h.detail = "needs_key", "set GFW_TOKEN"
        elif self.unauthorized:
            h.status, h.detail = "down", "unauthorized"
        elif self.last_error_at is not None and (
            self.last_ok is None or self.last_error_at >= self.last_ok
        ):
            h.status, h.detail = "degraded", self.last_error
        return h

    async def fetch(self, aoi: AOI, dataset: str) -> tuple[list[GeoEvent], int]:
        """All pages for one AOI x dataset over the lookback window."""
        assert self._token
        end = self._now()
        start = end - timedelta(days=self.lookback_days)
        s, w, n, e = bbox_for_radius(aoi.lat, aoi.lon, aoi.radius_nm)[0]
        geometry = {
            "type": "Polygon",
            "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
        }
        events: list[GeoEvent] = []
        dropped = 0
        offset = 0
        for _ in range(MAX_PAGES):
            params = {
                "datasets[0]": DATASETS[dataset][0],
                "start-date": start.date().isoformat(),
                "end-date": end.date().isoformat(),
                "limit": PAGE_LIMIT,
                "offset": offset,
                "geometry": json.dumps(geometry, separators=(",", ":")),
            }
            resp = await self.client.get(
                f"{self.base_url}/events",
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            payload = resp.json()
            page, bad = parse_gfw_events_counted(payload, dataset)
            events.extend(
                ev
                for ev in page
                if ev.lat is not None
                and ev.lon is not None
                and within_radius_nm(ev.lat, ev.lon, aoi.lat, aoi.lon, aoi.radius_nm * 1.2)
            )
            dropped += bad
            nxt = payload.get("nextOffset")
            if not isinstance(nxt, int) or nxt <= offset or not payload.get("entries"):
                break
            offset = nxt
        return events, dropped

    async def poll_once(self) -> list[GeoEvent]:
        if not self._token:
            return []
        out: dict[str, GeoEvent] = {}
        for aoi in self.aois:
            for ds in self.datasets:
                try:
                    events, dropped = await self.fetch(aoi, ds)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (401, 403):
                        self.unauthorized = True
                        self._fail("unauthorized")
                        return list(out.values())  # every other request would fail the same way
                    self._fail(f"HTTP {e.response.status_code}")
                    continue
                except (httpx.HTTPError, CircuitOpenError, ValueError) as e:
                    self._fail(f"{type(e).__name__}: {e}")
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
        if not self._token:
            log.info("gfw_needs_key", source=self.name)
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


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    env = str(cfg.get("auth", "GFW_TOKEN"))
    return GfwConnector(
        key,
        str(cfg["base"]),
        settings.aois,
        os.environ.get(env) or None,
        datasets=list(cfg.get("datasets", ["gaps", "loitering", "encounters"])),
        sar=bool(cfg.get("sar", False)),
        lookback_days=int(cfg.get("lookback_days", 3)),
        poll_s=float(cfg.get("poll_s", 21600)),
        rps=float(cfg.get("rps", 1)),
        reliability=SourceReliability(cfg.get("reliability", "B")),
        expected_latency_s=int(cfg.get("latency_s", 86400)),
    )
