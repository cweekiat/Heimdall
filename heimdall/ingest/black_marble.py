"""NASA Black Marble VNP46A2 connector: download a tile, reduce it to per-H3-r4 stats, delete it.

ADR-007 s2: rasters are reduced, never stored. Only `night_lights` GeoEvents leave this module;
no pixel arrays reach the Lake, Store or API. Keyed (ADR-004): without EARTHDATA_TOKEN the
connector makes no request and reports needs_key. The token goes only in the Authorization header.
NOTE: collection, latency and the QF/cloud bit layout are priors, to verify in task 016.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import h5py
import httpx
import numpy as np
from pydantic import BaseModel

from heimdall.config import AOI, Settings, secret
from heimdall.geo import bbox_for_radius, h3_cell, h3_center
from heimdall.ingest.adsb import SlidingCounter
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.logging import RECORDS_IN, get_logger
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceHealth, SourceReliability

log = get_logger("heimdall.black_marble")

GRID = "HDFEOS/GRIDS/VNP_Grid_DNB/Data Fields"
NTL = "DNB_BRDF-Corrected_NTL"  # never Gap_Filled_* (ADR-007 s2: gap-filled pixels are invented)
QF = "Mandatory_Quality_Flag"
CLOUD = "QF_Cloud_Mask"
SNOW = "Snow_Flag"
TILE_DEG = 10.0
_TILE_RE = re.compile(r"\.(h\d\dv\d\d)\.")
DEFAULT_CFG: dict[str, Any] = {"good_qf": [0, 1], "keep_failed_h": 24}


def tiles_for_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    """Tiles ('hXXvYY', 10 degree grid) covering a (south, west, north, east) box."""
    south, west, north, east = bbox
    h0 = min(35, max(0, int((west + 180.0) // TILE_DEG)))
    h1 = min(35, max(h0, int(np.ceil((east + 180.0) / TILE_DEG)) - 1))
    v0 = min(17, max(0, int((90.0 - north) // TILE_DEG)))
    v1 = min(17, max(v0, int(np.ceil((90.0 - south) / TILE_DEG)) - 1))
    return [f"h{h:02d}v{v:02d}" for v in range(v0, v1 + 1) for h in range(h0, h1 + 1)]


def tiles_for_aois(aois: Sequence[AOI]) -> list[str]:
    out: set[str] = set()
    for a in aois:
        for bb in bbox_for_radius(a.lat, a.lon, a.radius_nm):
            out.update(tiles_for_bbox(bb))
    return sorted(out)


def _tile_of(path: Path) -> tuple[int, int]:
    m = _TILE_RE.search(path.name)
    if m is None:
        raise ValueError(f"no tile id in {path.name}")
    return int(m.group(1)[1:3]), int(m.group(1)[4:6])


def _dataset(f: h5py.File, name: str) -> h5py.Dataset:
    ds = f[f"{GRID}/{name}"]
    if not isinstance(ds, h5py.Dataset):
        raise ValueError(f"{name} is not a dataset")
    return ds


def _attr(ds: h5py.Dataset, key: str, default: float) -> float:
    v = ds.attrs.get(key, default)
    return float(np.asarray(v).reshape(-1)[0])


def reduce_tile(
    path: Path, day: date | datetime, aois: Sequence[AOI], cfg: Mapping[str, Any]
) -> list[GeoEvent]:
    """Reduce one VNP46A2 tile to `night_lights` GeoEvents for H3 r4 cells centred in an AOI."""
    good_qf = [int(q) for q in cfg.get("good_qf", DEFAULT_CFG["good_qf"])]
    product = str(cfg.get("product", "VNP46A2"))
    d = day.date() if isinstance(day, datetime) else day
    h, v = _tile_of(path)
    with h5py.File(path, "r") as f:
        ntl_ds = _dataset(f, NTL)
        scale, offset = _attr(ntl_ds, "scale_factor", 1.0), _attr(ntl_ds, "add_offset", 0.0)
        fill = _attr(ntl_ds, "_FillValue", 65535.0)
        raw = ntl_ds[()]
        qf, cloud, snow = _dataset(f, QF)[()], _dataset(f, CLOUD)[()], _dataset(f, SNOW)[()]
    if raw.ndim != 2 or not (raw.shape == qf.shape == cloud.shape == snow.shape):
        raise ValueError("dataset shape mismatch")
    lats = 90.0 - TILE_DEG * v - (np.arange(raw.shape[0]) + 0.5) * (TILE_DEG / raw.shape[0])
    lons = -180.0 + TILE_DEG * h + (np.arange(raw.shape[1]) + 0.5) * (TILE_DEG / raw.shape[1])
    rad = raw.astype(np.float64) * scale + offset
    cloud_cls = (cloud.astype(np.uint16) >> 6) & 3  # Cloud_Detection_Results: 0/1 clear, 2/3 cloudy
    valid = (
        (raw != fill)
        & np.isfinite(rad)
        & (rad >= 0)
        & np.isin(qf, good_qf)
        & (cloud_cls <= 1)
        & (snow == 0)
    )
    cloudy = cloud_cls >= 2

    # only pixels inside an AOI bbox need an H3 lookup (keeps a 2400x2400 tile tractable)
    region = np.zeros(raw.shape, bool)
    for a in aois:
        for s, w, n, e in bbox_for_radius(a.lat, a.lon, a.radius_nm):
            region[np.ix_((lats >= s) & (lats <= n), (lons >= w) & (lons <= e))] = True
    groups: dict[str, list[tuple[int, int]]] = {}
    for i, j in zip(*np.nonzero(region), strict=True):
        groups.setdefault(h3_cell(float(lats[i]), float(lons[j]), 4), []).append((int(i), int(j)))

    events: list[GeoEvent] = []
    for cell, px in sorted(groups.items()):
        clat, clon = h3_center(cell)
        if not any(a.contains(clat, clon) for a in aois):
            continue
        n_total = len(px)  # > 0 by construction
        ii = np.fromiter((p[0] for p in px), int, n_total)
        jj = np.fromiter((p[1] for p in px), int, n_total)
        vals = rad[ii, jj][valid[ii, jj]]
        n_valid = int(vals.size)
        attrs: dict[str, Any] = {
            "role": "signal",
            "h3_r4": cell,
            "date": d.isoformat(),
            "product": product,
            "n_valid": n_valid,
            "n_total": n_total,
            "valid_frac": n_valid / n_total,
            "cloud_frac": float(cloudy[ii, jj].sum()) / n_total,
            "median": float(np.median(vals)) if n_valid else None,
            "p90": float(np.percentile(vals, 90)) if n_valid else None,
        }
        events.append(
            GeoEvent(
                id=f"black_marble|{product}|{d.isoformat()}|{cell}",
                ts=datetime(d.year, d.month, d.day, tzinfo=UTC),
                source="black_marble",
                domain=Domain.SPACE,
                kind="night_lights",
                lat=clat,
                lon=clon,
                title="night lights",
                value=float(vals.mean()) if n_valid else None,
                attrs=attrs,
                h3_r4=cell,
                h3_r6=h3_cell(clat, clon, 6),
            )
        )
    return events


def merge_events(events: Sequence[GeoEvent]) -> list[GeoEvent]:
    """Cells cut by a tile edge appear once per tile: keep the one with the most pixels."""
    best: dict[str, GeoEvent] = {}
    for ev in events:
        cur = best.get(ev.id)
        if cur is None or ev.attrs["n_total"] > cur.attrs["n_total"]:
            best[ev.id] = ev
    return list(best.values())


class BlackMarbleConnector:
    domain = Domain.SPACE

    def __init__(
        self,
        name: str,
        base_url: str,
        aois: Sequence[AOI],
        token: str | None,
        *,
        cfg: Mapping[str, Any] | None = None,
        cache_dir: Path = Path("data/cache/black_marble"),
        rps: float = 1.0,
        reliability: SourceReliability = SourceReliability.A,
        expected_latency_s: int = 172800,
        client: RateLimitedClient | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        c = {**DEFAULT_CFG, **(cfg or {})}
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.aois = list(aois)
        self._token = token or None
        self.cfg = c
        self.product = str(c.get("product", "VNP46A2"))
        self.collection = str(c.get("collection", "5200"))
        self.lookback_days = int(c.get("lookback_days", 5))
        self.poll_s = float(c.get("poll_s", 21600))
        self.keep_failed_s = float(c["keep_failed_h"]) * 3600.0
        self.cache_dir = cache_dir
        self.reliability = reliability
        self.expected_latency_s = expected_latency_s
        self._client = client
        self._rps = rps
        self._sleep, self._now = sleep, now
        self._stop = False
        self.unauthorized = False
        self.done: set[tuple[str, str]] = set()  # (date, tile) already reduced
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.n_ok = 0
        self.n_err = 0
        self.records_hour = SlidingCounter()

    @property
    def client(self) -> RateLimitedClient:
        if self._client is None:
            self._client = RateLimitedClient(self.name, rps=self._rps, timeout=120.0)
        return self._client

    def _fail(self, text: str) -> None:
        self.n_err += 1
        self.last_error = text[:200]
        self.last_error_at = self._now()
        log.warning("black_marble_failed", source=self.name, error=self.last_error)

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
            h.status, h.detail = "needs_key", "set EARTHDATA_TOKEN"
        elif self.unauthorized:
            h.status, h.detail = "down", "unauthorized"
        elif self.last_error_at is not None and (
            self.last_ok is None or self.last_error_at >= self.last_ok
        ):
            h.status, h.detail = "degraded", self.last_error
        return h

    def _day_url(self, d: date) -> str:
        doy = d.timetuple().tm_yday
        return (
            f"{self.base_url}/archive/allData/{self.collection}/{self.product}/{d.year}/{doy:03d}"
        )

    def cleanup_cache(self) -> None:
        """Delete leftover tiles (failed reduces) older than keep_failed_h."""
        if not self.cache_dir.is_dir():
            return
        cutoff = time.time() - self.keep_failed_s
        for p in self.cache_dir.glob("*.h5"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)

    async def fetch_day(self, d: date, tiles: Sequence[str]) -> list[GeoEvent]:
        assert self._token
        auth = {"Authorization": f"Bearer {self._token}"}
        base = self._day_url(d)
        resp = await self.client.get(f"{base}.json", headers=auth, follow_redirects=True)
        listing = resp.json()
        names = [
            str(x["name"])
            for x in (listing if isinstance(listing, list) else [])
            if isinstance(x, Mapping) and str(x.get("name", "")).endswith(".h5")
        ]
        out: list[GeoEvent] = []
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for tile in tiles:
            key = (d.isoformat(), tile)
            name = next((n for n in names if f".{tile}." in n), None)
            if key in self.done or name is None:
                continue  # not published yet (latency ~2 days): retried next poll
            dest = self.cache_dir / Path(name).name  # basename only: never trust listing paths
            try:
                await self.client.download(
                    f"{base}/{name}", dest, headers=auth, follow_redirects=True
                )
                out.extend(await asyncio.to_thread(reduce_tile, dest, d, self.aois, self.cfg))
            except (OSError, ValueError, KeyError) as e:  # bad tile: kept for keep_failed_h
                self._fail(f"{type(e).__name__}: {e}")
                continue
            dest.unlink(missing_ok=True)
            self.done.add(key)
        return out

    async def poll_once(self) -> list[GeoEvent]:
        if not self._token:
            return []
        self.cleanup_cache()
        tiles = tiles_for_aois(self.aois)
        today = self._now().date()
        out: list[GeoEvent] = []
        for k in range(1, self.lookback_days + 1):
            try:
                out.extend(await self.fetch_day(today - timedelta(days=k), tiles))
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    self.unauthorized = True
                    self._fail("unauthorized")
                    break
                if e.response.status_code != 404:  # 404 = day not published yet
                    self._fail(f"HTTP {e.response.status_code}")
                continue
            except (httpx.HTTPError, CircuitOpenError, ValueError) as e:
                self._fail(f"{type(e).__name__}: {e}")
                continue
            self.unauthorized = False
            self.n_ok += 1
            self.last_ok = self._now()
        events = merge_events(out)
        self.records_hour.add(len(events))
        RECORDS_IN.labels(self.name).inc(len(events))
        return events

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        if not self._token:
            log.info("black_marble_needs_key", source=self.name)
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
    return BlackMarbleConnector(
        key,
        str(cfg["base"]),
        settings.aois,
        secret(str(cfg.get("auth", "EARTHDATA_TOKEN"))),
        cfg=cfg,
        rps=float(cfg.get("rps", 1)),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 172800)),
    )
