"""Sentinel-1/2 STAC connector: item footprints as `imagery_footprint` GeoEvents.

METADATA ONLY. The item `assets` object is never read, and no asset/thumbnail/COG URL is ever
requested or stored (user decision 2026-09-30). `url` holds the item's own STAC link as text.
No key needed. Host and collections are config (verified in task 016).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from heimdall.config import AOI, Settings
from heimdall.geo import bbox_for_radius, h3_cell
from heimdall.ingest.adsb import SlidingCounter
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.logging import DROPPED, RECORDS_IN, get_logger
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceHealth, SourceReliability

log = get_logger("heimdall.stac")
PAGE_LIMIT = 100


def _dt(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _tok(v: Any, n: int = 40) -> str | None:
    return " ".join(v.split())[:n] or None if isinstance(v, str) else None


def _self_link(item: Mapping[str, Any]) -> str | None:
    for link in item.get("links") or []:
        if isinstance(link, Mapping) and link.get("rel") == "self":
            href = link.get("href")
            return href if isinstance(href, str) and href.startswith("https://") else None
    return None


def _centroid(geom: Mapping[str, Any]) -> tuple[float, float]:
    """Mean of the outer-ring vertices of a Polygon / first polygon of a MultiPolygon."""
    coords = geom.get("coordinates")
    if geom.get("type") == "MultiPolygon":
        coords = coords[0] if coords else None
    ring = coords[0] if geom.get("type") in ("Polygon", "MultiPolygon") and coords else None
    if not ring:
        raise ValueError("no ring")
    lons = [float(p[0]) for p in ring]
    lats = [float(p[1]) for p in ring]
    lat, lon = sum(lats) / len(lats), sum(lons) / len(lons)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("implausible footprint")
    return lat, lon


def item_to_event(item: Mapping[str, Any]) -> GeoEvent:
    """One STAC item -> GeoEvent from id, collection, geometry, chosen properties, self link."""
    iid, coll = item.get("id"), item.get("collection")
    if not isinstance(iid, str) or not iid or not isinstance(coll, str):
        raise ValueError("no id/collection")
    props = item.get("properties")
    props = props if isinstance(props, Mapping) else {}
    ts = _dt(props.get("datetime"))
    geom = item.get("geometry")
    if ts is None or not isinstance(geom, Mapping):
        raise ValueError("no datetime/geometry")
    url = _self_link(item)
    if url is None:
        raise ValueError("no self link")
    lat, lon = _centroid(geom)
    cloud = _num(props.get("eo:cloud_cover"))
    if cloud is not None and not 0 <= cloud <= 100:
        raise ValueError("cloud cover out of range")
    rel = props.get("sat:relative_orbit")
    return GeoEvent(
        id=f"stac|{iid}",
        ts=ts,
        source="stac",
        domain=Domain.SPACE,
        kind="imagery_footprint",
        lat=lat,
        lon=lon,
        geometry=dict(geom),
        title=f"{coll} footprint",
        url=url,
        value=cloud,
        attrs={
            "role": "context",
            "collection": coll[:40],
            "platform": _tok(props.get("platform")),
            "stac_item_id": iid[:120],
            "orbit_state": _tok(props.get("sat:orbit_state")),
            "relative_orbit": int(rel)
            if isinstance(rel, int) and not isinstance(rel, bool)
            else None,
            "cloud_cover": cloud,
        },
        h3_r4=h3_cell(lat, lon, 4),
        h3_r6=h3_cell(lat, lon, 6),
    )


def parse_stac_items(payload: Mapping[str, Any]) -> tuple[list[GeoEvent], int]:
    """(events, n_dropped) for one /search page."""
    feats = payload.get("features")
    out: list[GeoEvent] = []
    dropped = 0
    for it in feats if isinstance(feats, list) else []:
        try:
            out.append(item_to_event(it))
        except (ValueError, TypeError, AttributeError, IndexError, KeyError, ValidationError):
            dropped += 1
    return out, dropped


class StacConnector:
    domain = Domain.SPACE

    def __init__(
        self,
        name: str,
        base_url: str,
        aois: Sequence[AOI],
        *,
        collections: Sequence[str] = ("sentinel-1-grd",),
        lookback_days: int = 10,
        max_items: int = 500,
        poll_s: float = 21600.0,
        rps: float = 1.0,
        reliability: SourceReliability = SourceReliability.A,
        expected_latency_s: int = 86400,
        client: RateLimitedClient | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.aois = list(aois)
        self.collections = list(collections)
        self.lookback_days, self.max_items, self.poll_s = lookback_days, max_items, poll_s
        self.reliability = reliability
        self.expected_latency_s = expected_latency_s
        self._client = client
        self._rps = rps
        self._sleep, self._now = sleep, now
        self._stop = False
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.n_ok = 0
        self.n_err = 0
        self.n_dropped = 0
        self.records_hour = SlidingCounter()

    @property
    def client(self) -> RateLimitedClient:
        if self._client is None:
            self._client = RateLimitedClient(self.name, rps=self._rps)
        return self._client

    def _fail(self, text: str) -> None:
        self.n_err += 1
        self.last_error = text[:200]
        self.last_error_at = self._now()
        log.warning("stac_fetch_failed", source=self.name, error=self.last_error)

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
        return h

    def _same_host(self, href: str) -> bool:
        return urlsplit(href).netloc == urlsplit(self.base_url).netloc

    async def search(self, bbox: tuple[float, float, float, float]) -> tuple[list[GeoEvent], int]:
        """All pages (capped at max_items) for one (south, west, north, east) box."""
        s, w, n, e = bbox
        end = self._now()
        start = end - timedelta(days=self.lookback_days)
        body: dict[str, Any] = {
            "collections": self.collections,
            "bbox": [w, s, e, n],
            "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
            "limit": min(PAGE_LIMIT, self.max_items),
        }
        url = f"{self.base_url}/search"
        events: list[GeoEvent] = []
        dropped = seen = 0
        while True:
            payload = (await self.client.post(url, json=body)).json()
            page, bad = parse_stac_items(payload)
            feats = payload.get("features")
            seen += len(feats) if isinstance(feats, list) else 0
            events.extend(page)
            dropped += bad
            nxt = next(
                (
                    lk
                    for lk in payload.get("links") or []
                    if isinstance(lk, Mapping) and lk.get("rel") == "next"
                ),
                None,
            )
            href = nxt.get("href") if nxt else None
            # follow only same-host pagination (no new hosts, security rule); cap at max_items
            if not isinstance(href, str) or not self._same_host(href) or seen >= self.max_items:
                break
            nb = nxt.get("body") if nxt else None
            if isinstance(nb, Mapping):
                body = {**body, **nb} if nxt and nxt.get("merge") else dict(nb)
            url = href
        return events[: self.max_items], dropped

    async def poll_once(self) -> list[GeoEvent]:
        out: dict[str, GeoEvent] = {}
        for aoi in self.aois:
            for bb in bbox_for_radius(aoi.lat, aoi.lon, aoi.radius_nm):
                try:
                    events, dropped = await self.search(bb)
                except httpx.HTTPStatusError as e:
                    self._fail(f"HTTP {e.response.status_code}")
                    continue
                except (httpx.HTTPError, CircuitOpenError, ValueError) as e:
                    self._fail(f"{type(e).__name__}: {e}")
                    continue
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
    return StacConnector(
        key,
        str(cfg["base"]),
        settings.aois,
        collections=list(cfg.get("collections", ["sentinel-1-grd"])),
        lookback_days=int(cfg.get("lookback_days", 10)),
        max_items=int(cfg.get("max_items", 500)),
        poll_s=float(cfg.get("poll_s", 21600)),
        rps=float(cfg.get("rps", 1)),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 86400)),
    )
