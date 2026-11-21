"""NASA CYGNSS L1 noise floor -> `l1_noise` GeoEvents (, band L1 GNSS seen from orbit).

CYGNSS (7 working microsatellites, +-38 deg latitude) listens to GPS L1 reflections. Its nadir
antennas also hear any L1 jammer in the beam: the DDM noise floor rises and the `rfi_detected`
quality bit (noise-floor kurtosis test) fires. This connector turns that into per-H3 cell-day
values, independent of aircraft and ships.

Pipeline per L1 granule (one satellite-day, `cygNN.ddmi.sYYYYMMDD-...l1.power-brcs...`):
1. NASA CMR search (no key) -> the granule's OPeNDAP URL on an allowed host.
2. OPeNDAP DAP4 subset of five variables only (~8 MB instead of the ~1.15 GB file), keyed with
   EARTHDATA_TOKEN in the Authorization header (no token -> no request, needs_key).
3. `reduce_arrays`: drop invalid DDMs (black body, test pattern, idle channel, CRC/reconfig);
   normalise each DDM channel by its own **open-ocean median of the whole day** (removes per-
   satellite gain/temperature differences); per H3 cell inside `bbox`: n, median excess dB, open-
   ocean count, rfi_detected count. The small reduction is cached as JSON, the subset deleted
   ( s2: rasters are reduced, never stored).
4. `merge_day` combines the satellites of one UTC day (n-weighted); `baseline` attaches the
   cell's own trailing median/MAD over earlier cached days. One event per cell-day, id
   `cygnss|YYYY-MM-DD|cell`; a late granule re-emits that day with the same ids (upsert).

Fields (L1 v3.2 data dictionary 148-0346-11, checked live 2026-10-01): `ddm_noise_floor`
(sample, ddm) counts; `quality_flags` bits 16 black_body_ddm, 32 ddmi_reconfigured, 64
spacewire_crc_invalid, 128 ddm_is_test_pattern, 256 channel_idle, 1024 sp_over_land, 2048
sp_very_near_land, 4096 sp_near_land, 131072 rfi_detected; `sp_lat`/`sp_lon` (lon 0-360).
The noise floor is mapped at the specular point, so the location is only as good as the antenna
footprint (tens to hundreds of km).
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import h3
import h5py
import numpy as np

from heimdall.config import Settings, secret
from heimdall.ingest.poll import PollConnector, Unit, parse_dt
from heimdall.ingest.wspr import _bbox
from heimdall.logging import get_logger
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

log = get_logger("heimdall.cygnss")

SHORT_NAME = "CYGNSS_L1_V3.2"
VARIABLES = ("ddm_timestamp_utc", "sp_lat", "sp_lon", "ddm_noise_floor", "quality_flags")
INVALID = 16 | 32 | 64 | 128 | 256  # black body, reconfigured, CRC, test pattern, idle channel
LAND = 1024 | 2048 | 4096  # over land, < 25 km, < 50 km from land
RFI = 131072  # rfi_detected: noise-floor kurtosis deviates from Gaussian by > 1.0
DEFAULT_BBOX = (25.0, -40.0, 160.0, 40.0)  # Middle East (control) .. W Pacific; CYGNSS sees +-38
TITLE_RE = re.compile(
    r"^(cyg0[1-8])\.ddmi\.s(\d{8})-\d{6}-e\d{8}-\d{6}\.l1\.power-brcs\.a\d{2}\.d\d{2}$"
)
MIN_OCEAN_REF = 1000  # open-ocean DDMs per channel needed to normalise a granule


def parse_title(title: str) -> tuple[str, date] | None:
    """Granule title -> (satellite, UTC day), or None for anything unexpected (used in paths)."""
    m = TITLE_RE.match(title)
    if not m:
        return None
    try:
        return m.group(1), datetime.strptime(m.group(2), "%Y%m%d").date()
    except ValueError:
        return None


def reduce_arrays(
    lat: np.ndarray,
    lon: np.ndarray,
    nf: np.ndarray,
    q: np.ndarray,
    *,
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
    h3_res: int = 3,
) -> dict[str, Any] | None:
    """One granule's (sample, ddm) arrays -> {"ref": [...], "cells": {cell: [n, db, n_ocean,
    n_rfi]}}; None when the day has too little open ocean to normalise a channel."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    nf, q = np.asarray(nf, float), np.asarray(q).astype(np.int64)
    n_ch = lat.shape[1] if lat.ndim == 2 else 1
    ch = np.broadcast_to(np.arange(n_ch), lat.shape).ravel() if lat.ndim == 2 else None
    lat, lon, nf, q = lat.ravel(), lon.ravel(), nf.ravel(), q.ravel()
    lon = np.where(lon > 180.0, lon - 360.0, lon)
    ok = (
        ((q & INVALID) == 0)
        & np.isfinite(nf)
        & (nf > 0)
        & np.isfinite(lat)
        & np.isfinite(lon)
        & (np.abs(lat) <= 90)
    )
    ocean = ok & ((q & LAND) == 0)
    chan = ch if ch is not None else np.zeros(lat.shape, dtype=int)
    ref = np.full(n_ch, np.nan)
    for c in range(n_ch):
        sel = ocean & (chan == c)
        if int(sel.sum()) >= MIN_OCEAN_REF:
            ref[c] = float(np.median(nf[sel]))
    if not np.isfinite(ref).any():
        return None
    w, s, e, n = bbox
    keep = ok & np.isfinite(ref[chan]) & (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    idx = np.flatnonzero(keep)
    cells: dict[str, list[float]] = {}
    if idx.size:
        db = 10.0 * np.log10(nf[idx] / ref[chan[idx]])
        keys = np.array(
            [
                h3.latlng_to_cell(float(a), float(b), h3_res)
                for a, b in zip(lat[idx], lon[idx], strict=True)
            ]
        )
        oc, rf = ocean[idx], (q[idx] & RFI) != 0
        order = np.argsort(keys, kind="stable")
        keys, db, oc, rf = keys[order], db[order], oc[order], rf[order]
        uniq, start = np.unique(keys, return_index=True)
        bounds = [*start.tolist(), len(keys)]
        for i, cell in enumerate(uniq.tolist()):
            a, b = bounds[i], bounds[i + 1]
            cells[str(cell)] = [
                b - a,
                round(float(np.median(db[a:b])), 3),
                int(oc[a:b].sum()),
                int(rf[a:b].sum()),
            ]
    return {
        "ref": [None if not np.isfinite(r) else round(float(r), 1) for r in ref],
        "cells": cells,
    }


def reduce_file(path: Path, **kw: Any) -> dict[str, Any] | None:
    with h5py.File(path, "r") as f:
        return reduce_arrays(
            f["sp_lat"][...],
            f["sp_lon"][...],
            f["ddm_noise_floor"][...],
            f["quality_flags"][...],
            **kw,
        )


@dataclass
class CellDay:
    n: int = 0
    db_sum: float = 0.0  # sum of n x granule median (n-weighted mean over satellites)
    n_ocean: int = 0
    n_rfi: int = 0
    sats: set[str] = field(default_factory=set)

    @property
    def db(self) -> float:
        return self.db_sum / self.n if self.n else 0.0


def merge_day(reductions: Iterable[Mapping[str, Any]]) -> dict[str, CellDay]:
    """Reductions of one UTC day (any satellites) -> per-cell values."""
    out: dict[str, CellDay] = {}
    for red in reductions:
        sat = str(red.get("sat", "?"))
        for cell, v in (red.get("cells") or {}).items():
            n, db, n_oc, n_rfi = int(v[0]), float(v[1]), int(v[2]), int(v[3])
            c = out.setdefault(cell, CellDay())
            c.n += n
            c.db_sum += n * db
            c.n_ocean += n_oc
            c.n_rfi += n_rfi
            c.sats.add(sat)
    return out


def baseline(
    history: Sequence[CellDay | None], min_samples: int
) -> tuple[float | None, float | None, int, float | None]:
    """(median dB, MAD dB, days used, median rfi fraction) over a cell's earlier days."""
    days = [c for c in history if c is not None and c.n >= min_samples]
    if not days:
        return None, None, 0, None
    vals = [c.db for c in days]
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    rfi = statistics.median(c.n_rfi / c.n for c in days)
    return med, mad, len(days), rfi


def day_events(
    day: date,
    cells: Mapping[str, CellDay],
    history: Sequence[Mapping[str, CellDay]],
    *,
    min_samples: int = 20,
    source: str = "cygnss",
) -> list[GeoEvent]:
    """One `l1_noise` event per cell with >= min_samples DDMs; `history` = earlier days."""
    t0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
    out: list[GeoEvent] = []
    for cell, c in sorted(cells.items()):
        if c.n < min_samples:
            continue
        med, mad, nd, rfi0 = baseline([h.get(cell) for h in history], min_samples)
        lat, lon = h3.cell_to_latlng(cell)
        out.append(
            GeoEvent(
                id=f"cygnss|{day.isoformat()}|{cell}",
                ts=t0,
                valid_from=t0,
                valid_to=t0 + timedelta(days=1),
                source=source,
                domain=Domain.SPECTRUM,
                kind="l1_noise",
                lat=lat,
                lon=lon,
                title="CYGNSS L1 noise floor",
                value=round(c.db, 3),
                attrs={
                    "role": "signal",
                    "day": day.isoformat(),
                    "h3": cell,
                    "n": c.n,
                    "n_sats": len(c.sats),
                    "excess_db": round(c.db, 3),
                    "ocean_frac": round(c.n_ocean / c.n, 3),
                    "rfi_frac": round(c.n_rfi / c.n, 4),
                    "base_db": None if med is None else round(med, 3),
                    "base_mad_db": None if mad is None else round(mad, 3),
                    "base_days": nd,
                    "base_rfi_frac": None if rfi0 is None else round(rfi0, 4),
                },
            )
        )
    return out


def parse_cmr(payload: Any, allowed_hosts: Sequence[str]) -> list[tuple[str, datetime, str]]:
    """CMR granules.json -> [(title, start, OPeNDAP granule URL on an allowed https host)]."""
    entries = payload.get("feed", {}).get("entry") if isinstance(payload, Mapping) else None
    out: list[tuple[str, datetime, str]] = []
    for g in entries if isinstance(entries, list) else []:
        if not isinstance(g, Mapping):
            continue
        title, ts = g.get("title"), parse_dt(g.get("time_start"))
        if not isinstance(title, str) or ts is None or parse_title(title) is None:
            continue
        raw = g.get("links")
        links: list[Any] = raw if isinstance(raw, list) else []
        for lk in links:
            href = lk.get("href") if isinstance(lk, Mapping) else None
            if not isinstance(href, str):
                continue
            u = urlsplit(href)
            if (
                u.scheme == "https"
                and (u.hostname or "") in allowed_hosts
                and "/granules/" in u.path
                and u.path.endswith("/" + title)
                and not u.query
            ):
                out.append((title, ts, href))
                break
    return out


class CygnssConnector(PollConnector):
    domain = Domain.SPECTRUM
    token_name = "EARTHDATA_TOKEN"

    def __init__(
        self,
        name: str,
        cmr_url: str,
        *,
        short_name: str = SHORT_NAME,
        bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
        lookback_days: float = 4,
        baseline_days: int = 14,
        max_granules: int = 8,
        h3_res: int = 3,
        min_samples: int = 20,
        allowed_hosts: Sequence[str] = ("opendap.earthdata.nasa.gov",),
        cache_dir: Path = Path("data/cache/cygnss"),
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.cmr_url = cmr_url.rstrip("/")
        self.short_name, self.bbox = short_name, bbox
        self.lookback_days, self.baseline_days = lookback_days, baseline_days
        self.max_granules, self.h3_res, self.min_samples = max_granules, h3_res, min_samples
        self.allowed_hosts = tuple(allowed_hosts)
        self.cache_dir = cache_dir
        self.red_dir = cache_dir / "reduced"

    # ------------------------------------------------------------------ cache
    def cached(self, title: str) -> Path:
        return self.red_dir / f"{title}.json"  # title validated by parse_title

    def load_day(self, day: date) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.red_dir.glob(f"cyg0?.ddmi.s{day:%Y%m%d}-*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
        return out

    def events_for(self, day: date) -> list[GeoEvent]:
        cells = merge_day(self.load_day(day))
        hist = [
            merge_day(self.load_day(day - timedelta(days=k)))
            for k in range(1, self.baseline_days + 1)
        ]
        return day_events(day, cells, hist, min_samples=self.min_samples, source=self.name)

    def prune(self, today: date) -> None:
        keep_from = today - timedelta(days=self.baseline_days + self.lookback_days + 3)
        for p in self.red_dir.glob("*.json"):
            meta = parse_title(p.stem)
            if meta is not None and meta[1] < keep_from:
                p.unlink(missing_ok=True)

    # ------------------------------------------------------------------ network
    async def search(self, start: datetime, end: datetime) -> list[tuple[str, datetime, str]]:
        w, s, e, n = self.bbox
        r = await self.client.get(
            f"{self.cmr_url}/search/granules.json",
            params={
                "short_name": self.short_name,
                "bounding_box": f"{w},{s},{e},{n}",
                "temporal": f"{start:%Y-%m-%dT%H:%M:%SZ},{end:%Y-%m-%dT%H:%M:%SZ}",
                "sort_key": "-start_date",
                "page_size": 100,
            },
        )
        return parse_cmr(r.json(), self.allowed_hosts)

    async def fetch(self, title: str, href: str) -> dict[str, Any] | None:
        """OPeNDAP subset -> reduction (cached as JSON); the subset file is always deleted."""
        meta = parse_title(title)
        if meta is None:
            return None
        self.red_dir.mkdir(parents=True, exist_ok=True)
        dest = self.cache_dir / f"{title}.nc4"
        try:
            await self.client.download(
                f"{href}.dap.nc4",
                dest,
                params={"dap4.ce": ";".join("/" + v for v in VARIABLES)},
                headers={"Authorization": f"Bearer {self._token}"},
                follow_redirects=True,
            )
            red = reduce_file(dest, bbox=self.bbox, h3_res=self.h3_res)
        finally:
            dest.unlink(missing_ok=True)
        if red is None:
            return None
        red.update(title=title, sat=meta[0], day=meta[1].isoformat())
        self.cached(title).write_text(json.dumps(red, separators=(",", ":")))
        return red

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            now = self._now().astimezone(UTC)
            found = await self.search(now - timedelta(days=self.lookback_days), now)
            todo = [g for g in found if not self.cached(g[0]).exists()][: self.max_granules]
            days: set[date] = set()
            dropped = 0
            for title, _, href in todo:
                try:
                    red = await self.fetch(title, href)
                except (OSError, KeyError, ValueError):
                    red = None
                if red is None:
                    dropped += 1
                    continue
                days.add(date.fromisoformat(red["day"]))
            events = [e for d in sorted(days) for e in self.events_for(d)]
            self.prune(now.date())
            if todo:
                log.info("cygnss_poll", granules=len(todo), days=len(days), events=len(events))
            return events, dropped

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return CygnssConnector(
        key,
        str(cfg["base"]),
        token=secret(str(cfg.get("auth", "EARTHDATA_TOKEN"))),
        short_name=str(cfg.get("short_name", SHORT_NAME)),
        bbox=_bbox(cfg.get("bbox")) if cfg.get("bbox") else DEFAULT_BBOX,
        lookback_days=float(cfg.get("lookback_days", 4)),
        baseline_days=int(cfg.get("baseline_days", 14)),
        max_granules=int(cfg.get("max_granules", 8)),
        h3_res=int(cfg.get("h3_res", 3)),
        min_samples=int(cfg.get("min_samples", 20)),
        allowed_hosts=tuple(cfg.get("allowed_hosts", ("opendap.earthdata.nasa.gov",))),
        cache_dir=Path(str(cfg.get("cache_dir", "data/cache/cygnss"))),
        poll_s=float(cfg.get("poll_s", 10800)),
        rps=float(cfg.get("rps", 0.2)),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 259200)),
    )
