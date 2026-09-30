"""WSPR Live (public ClickHouse over HTTPS, no key) -> `hf_spots` GeoEvents (ADR-008, P6 band HF).

Aggregation runs server-side, so one poll moves a few thousand rows, not ~300k raw spots/hour:
spots are grouped per (10-min bin, band, great-circle-midpoint grid cell) inside `bbox`, with
spot count, distinct paths/stations and SNR statistics. Bins are re-queried for `lookback_min`
and upserted by stable id, so late-arriving spots complete earlier bins. Only numbers derived by
this module go into the SQL (no user or remote text).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from heimdall.config import Settings
from heimdall.ingest.hf_common import in_bbox, spot_id
from heimdall.ingest.poll import PollConnector, Unit, num
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

SQL = """
SELECT toStartOfInterval(time, INTERVAL {bin_min} MINUTE) AS t, band,
  round(mlat / {g}) * {g} AS glat, round(mlon / {g}) * {g} AS glon,
  count() AS n, uniqExact(tx_sign, rx_sign) AS paths, median(snr) AS snr_med,
  avg(snr) AS snr_mean, uniqExact(rx_sign) AS n_rx, uniqExact(tx_sign) AS n_tx,
  avg(distance) AS dist_km
FROM (
  SELECT time, band, snr, distance, tx_sign, rx_sign,
    radians(tx_lat) AS la1, radians(rx_lat) AS la2, radians(rx_lon - tx_lon) AS dl,
    cos(la2) * cos(dl) AS bxx, cos(la2) * sin(dl) AS byy,
    degrees(atan2(sin(la1) + sin(la2),
      sqrt((cos(la1) + bxx) * (cos(la1) + bxx) + byy * byy))) AS mlat,
    ((tx_lon + degrees(atan2(byy, cos(la1) + bxx)) + 540) % 360) - 180 AS mlon
  FROM wspr.rx
  WHERE time >= '{t0}' AND time < '{t1}' AND distance >= {min_km} AND distance <= {max_km}
)
WHERE mlat BETWEEN {s} AND {n} AND mlon BETWEEN {w} AND {e}
GROUP BY t, band, glat, glon
FORMAT JSON
"""

DEFAULT_BBOX = (60.0, -20.0, 160.0, 50.0)  # Indo-Pacific: SE Asia AOIs + paths reflecting over them


def build_query(
    t0: datetime,
    t1: datetime,
    *,
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
    grid_deg: float = 2.0,
    bin_min: int = 10,
    min_km: int = 500,
    max_km: int = 12000,
) -> str:
    w, s, e, n = (float(v) for v in bbox)
    fmt = "%Y-%m-%d %H:%M:%S"
    return SQL.format(
        bin_min=int(bin_min),
        g=float(grid_deg),
        t0=t0.astimezone(UTC).strftime(fmt),
        t1=t1.astimezone(UTC).strftime(fmt),
        min_km=int(min_km),
        max_km=int(max_km),
        w=w,
        s=s,
        e=e,
        n=n,
    )


def parse_wspr(
    payload: Any,
    *,
    bin_min: int = 10,
    grid_deg: float = 2.0,
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
) -> tuple[list[GeoEvent], int]:
    """ClickHouse `FORMAT JSON` body -> (events, n_dropped)."""
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return [], 0
    out: list[GeoEvent] = []
    dropped = 0
    for r in rows:
        if not isinstance(r, Mapping):
            dropped += 1
            continue
        try:
            t = datetime.strptime(str(r.get("t")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            dropped += 1
            continue
        band = r.get("band")
        glat, glon, n = num(r.get("glat")), num(r.get("glon")), num(r.get("n"))
        if not isinstance(band, int) or glat is None or glon is None or not n:
            dropped += 1
            continue
        if not in_bbox(glat, glon, bbox):
            dropped += 1
            continue
        out.append(
            GeoEvent(
                id=spot_id("wspr", t, band, glat, glon),
                ts=t,
                source="wspr_live",
                domain=Domain.SPECTRUM,
                kind="hf_spots",
                lat=glat,
                lon=glon,
                valid_from=t,
                valid_to=t + timedelta(minutes=bin_min),
                title=f"WSPR {band} MHz spots",
                value=n,
                attrs={
                    "role": "signal",
                    "band_mhz": band,
                    "mode": "WSPR",
                    "n": int(n),
                    "paths": int(num(r.get("paths")) or 0),
                    "n_rx": int(num(r.get("n_rx")) or 0),
                    "n_tx": int(num(r.get("n_tx")) or 0),
                    "snr_med": num(r.get("snr_med")),
                    "snr_mean": num(r.get("snr_mean")),
                    "mean_dist_km": num(r.get("dist_km")),
                    "grid_deg": grid_deg,
                    "geo": "path_midpoint",
                },
            )
        )
    return out, dropped


class WsprConnector(PollConnector):
    domain = Domain.SPECTRUM

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
        grid_deg: float = 2.0,
        bin_min: int = 10,
        lookback_min: int = 60,
        settle_min: int = 5,
        min_km: int = 500,
        max_km: int = 12000,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.base_url = base_url.rstrip("/")
        self.bbox, self.grid_deg, self.bin_min = bbox, grid_deg, bin_min
        self.lookback_min, self.settle_min = lookback_min, settle_min
        self.min_km, self.max_km = min_km, max_km

    def window(self) -> tuple[datetime, datetime]:
        """[t0, t1): whole bins, ending `settle_min` before now (upload lag)."""
        end = self._now().astimezone(UTC) - timedelta(minutes=self.settle_min)
        end = end.replace(minute=end.minute - end.minute % self.bin_min, second=0, microsecond=0)
        return end - timedelta(minutes=self.lookback_min), end

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            t0, t1 = self.window()
            q = build_query(
                t0,
                t1,
                bbox=self.bbox,
                grid_deg=self.grid_deg,
                bin_min=self.bin_min,
                min_km=self.min_km,
                max_km=self.max_km,
            )
            r = await self.client.get(f"{self.base_url}/", params={"query": q})
            return parse_wspr(
                r.json(), bin_min=self.bin_min, grid_deg=self.grid_deg, bbox=self.bbox
            )

        return [run]


def _bbox(v: Any) -> tuple[float, float, float, float]:
    if isinstance(v, list | tuple) and len(v) == 4:
        w, s, e, n = (float(x) for x in v)
        return w, s, e, n
    return DEFAULT_BBOX


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return WsprConnector(
        key,
        str(cfg["base"]),
        bbox=_bbox(cfg.get("bbox")),
        grid_deg=float(cfg.get("grid_deg", 2.0)),
        bin_min=int(cfg.get("bin_min", 10)),
        lookback_min=int(cfg.get("lookback_min", 60)),
        settle_min=int(cfg.get("settle_min", 5)),
        min_km=int(cfg.get("min_km", 500)),
        max_km=int(cfg.get("max_km", 12000)),
        poll_s=float(cfg.get("poll_s", 600)),
        rps=float(cfg.get("rps", 0.2)),
        reliability=SourceReliability(cfg.get("reliability", "B")),
        expected_latency_s=int(cfg.get("latency_s", 900)),
    )
