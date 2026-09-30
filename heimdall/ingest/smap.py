"""NASA SMAP L-band radiometer RFI -> `l_band_rfi` GeoEvents (ADR-008, band L 1.4 GHz).

SMAP listens in the protected 1400-1427 MHz band and flags footprints where its RFI detectors fire.
Persistent flags on the ground mark L-band emitters (e.g. long-range air-surveillance radars); new
flags mark change. Granules are found with NASA CMR (no key), downloaded from NSIDC Earthdata Cloud
with EARTHDATA_TOKEN (keyed, ADR-004: without it the connector makes no request and reports
needs_key), reduced to per-H3-cell counts inside `bbox`, and deleted (ADR-007 s2: rasters are
reduced, never stored). The token goes only in the Authorization header; httpx drops it on the
cross-host redirect to the signed S3 URL.

Fields (SMAP L1B TB, group `Brightness_Temperature`): tb_lat, tb_lon, tb_qual_flag_h/v, tb_h/v.
The RFI bit is read from the dataset's `flag_meanings`/`flag_masks` attributes when present; the
config `rfi_bit` (default 2, per the L1B product spec) is the fallback. Verify with
`uv run python scripts/smap_inspect.py <file.h5>` once a token is set.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import h3
import h5py
import numpy as np

from heimdall.config import Settings, secret
from heimdall.ingest.poll import PollConnector, Unit, parse_dt
from heimdall.ingest.wspr import DEFAULT_BBOX, _bbox
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

GROUP = "Brightness_Temperature"
FILL = -9990.0  # SMAP fill is -9999; anything below this is missing


def rfi_mask(ds: h5py.Dataset, fallback_bit: int) -> int:
    """Bit mask for 'RFI detected' from flag_meanings/flag_masks, else 1 << fallback_bit."""
    meanings = ds.attrs.get("flag_meanings")
    masks = ds.attrs.get("flag_masks")
    if meanings is not None and masks is not None:
        names = (meanings.decode() if isinstance(meanings, bytes) else str(meanings)).split()
        for name, m in zip(names, np.atleast_1d(masks), strict=False):
            if re.search(r"rfi.*detect", name, re.I):
                return int(m)
    return 1 << fallback_bit


def reduce_granule(
    path: Path,
    granule_id: str,
    ts: datetime,
    *,
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
    h3_res: int = 4,
    rfi_bit: int = 2,
    min_footprints: int = 3,
) -> list[GeoEvent]:
    """One SMAP L1B TB file -> per-H3-cell RFI counts inside bbox."""
    w, s, e, n = bbox
    with h5py.File(path, "r") as f:
        g = f[GROUP]
        lat = np.asarray(g["tb_lat"][...], dtype=float).ravel()
        lon = np.asarray(g["tb_lon"][...], dtype=float).ravel()
        qh_ds, qv_ds = g["tb_qual_flag_h"], g["tb_qual_flag_v"]
        mh, mv = rfi_mask(qh_ds, rfi_bit), rfi_mask(qv_ds, rfi_bit)
        qh = np.asarray(qh_ds[...]).astype(np.int64).ravel()
        qv = np.asarray(qv_ds[...]).astype(np.int64).ravel()
        tbh = np.asarray(g["tb_h"][...], dtype=float).ravel()
    ok = (lat > FILL) & (lon > FILL) & (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
    cells: dict[str, list[int | float]] = {}
    for i in np.flatnonzero(ok):
        c = h3.latlng_to_cell(float(lat[i]), float(lon[i]), h3_res)
        acc = cells.setdefault(c, [0, 0, 0, 0.0, 0])  # n, rfi_h, rfi_v, tb_h_sum, tb_h_n
        acc[0] += 1
        acc[1] += int(bool(qh[i] & mh))
        acc[2] += int(bool(qv[i] & mv))
        if tbh[i] > FILL:
            acc[3] += float(tbh[i])
            acc[4] += 1
    out: list[GeoEvent] = []
    for c, (cnt, rh, rv, tsum, tn) in cells.items():
        if cnt < min_footprints:
            continue
        clat, clon = h3.cell_to_latlng(c)
        frac = max(rh, rv) / cnt
        out.append(
            GeoEvent(
                id=f"smap|{granule_id[:80]}|{c}",
                ts=ts,
                source="smap_rfi",
                domain=Domain.SPECTRUM,
                kind="l_band_rfi",
                lat=clat,
                lon=clon,
                title="SMAP L-band RFI" + (" flagged" if rh or rv else ""),
                value=frac,
                attrs={
                    "role": "signal",
                    "granule": granule_id[:80],
                    "h3": c,
                    "n_footprints": int(cnt),
                    "n_rfi_h": int(rh),
                    "n_rfi_v": int(rv),
                    "rfi_frac": frac,
                    "tb_h_mean_k": tsum / tn if tn else None,
                    "mask_h": mh,
                    "mask_v": mv,
                },
            )
        )
    return out


def parse_cmr(payload: Any, allowed_hosts: Sequence[str]) -> list[tuple[str, datetime, str]]:
    """CMR granules.json -> [(granule title, start time, https .h5 url on an allowed host)]."""
    entries = payload.get("feed", {}).get("entry") if isinstance(payload, Mapping) else None
    out: list[tuple[str, datetime, str]] = []
    for g in entries if isinstance(entries, list) else []:
        if not isinstance(g, Mapping):
            continue
        title, ts = g.get("title"), parse_dt(g.get("time_start"))
        raw_links = g.get("links")
        links: list[Any] = raw_links if isinstance(raw_links, list) else []
        href = next(
            (
                lk.get("href")
                for lk in links
                if isinstance(lk, Mapping)
                and isinstance(lk.get("href"), str)
                and lk["href"].endswith(".h5")
                and urlsplit(lk["href"]).scheme == "https"
                and (urlsplit(lk["href"]).hostname or "") in allowed_hosts
            ),
            None,
        )
        if isinstance(title, str) and ts is not None and isinstance(href, str):
            out.append((title, ts, href))
    return out


class SmapConnector(PollConnector):
    domain = Domain.SPECTRUM
    token_name = "EARTHDATA_TOKEN"

    def __init__(
        self,
        name: str,
        cmr_url: str,
        *,
        short_name: str = "SPL1BTB_NRT",
        bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
        lookback_h: float = 12,
        max_granules: int = 6,
        h3_res: int = 4,
        rfi_bit: int = 2,
        allowed_hosts: Sequence[str] = ("data.nsidc.earthdatacloud.nasa.gov",),
        cache_dir: Path = Path("data/cache/smap"),
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.cmr_url = cmr_url.rstrip("/")
        self.short_name, self.bbox = short_name, bbox
        self.lookback_h, self.max_granules = lookback_h, max_granules
        self.h3_res, self.rfi_bit = h3_res, rfi_bit
        self.allowed_hosts = tuple(allowed_hosts)
        self.cache_dir = cache_dir
        self.done: set[str] = set()

    async def search(self) -> list[tuple[str, datetime, str]]:
        now = self._now().astimezone(UTC)
        w, s, e, n = self.bbox
        t0 = (now - timedelta(hours=self.lookback_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = await self.client.get(
            f"{self.cmr_url}/search/granules.json",
            params={
                "short_name": self.short_name,
                "bounding_box": f"{w},{s},{e},{n}",
                "temporal": f"{t0},",
                "sort_key": "-start_date",
                "page_size": 50,
            },
        )
        return parse_cmr(r.json(), self.allowed_hosts)

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            todo = [g for g in await self.search() if g[0] not in self.done][: self.max_granules]
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            auth = {"Authorization": f"Bearer {self._token}"}
            events: list[GeoEvent] = []
            dropped = 0
            for title, ts, href in todo:
                dest = self.cache_dir / Path(urlsplit(href).path).name  # basename only
                try:
                    await self.client.download(href, dest, headers=auth, follow_redirects=True)
                    events += reduce_granule(
                        dest, title, ts, bbox=self.bbox, h3_res=self.h3_res, rfi_bit=self.rfi_bit
                    )
                    self.done.add(title)
                except (OSError, KeyError, ValueError):
                    dropped += 1
                finally:
                    dest.unlink(missing_ok=True)
            return events, dropped

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return SmapConnector(
        key,
        str(cfg["base"]),
        token=secret(str(cfg.get("auth", "EARTHDATA_TOKEN"))),
        short_name=str(cfg.get("short_name", "SPL1BTB_NRT")),
        bbox=_bbox(cfg.get("bbox")),
        lookback_h=float(cfg.get("lookback_h", 12)),
        max_granules=int(cfg.get("max_granules", 6)),
        h3_res=int(cfg.get("h3_res", 4)),
        rfi_bit=int(cfg.get("rfi_bit", 2)),
        allowed_hosts=tuple(cfg.get("allowed_hosts", ("data.nsidc.earthdatacloud.nasa.gov",))),
        cache_dir=Path(str(cfg.get("cache_dir", "data/cache/smap"))),
        poll_s=float(cfg.get("poll_s", 3600)),
        rps=float(cfg.get("rps", 0.2)),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 10800)),
    )
