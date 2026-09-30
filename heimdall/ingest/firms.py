"""NASA FIRMS active fires (area CSV API) -> `thermal` GeoEvents (ADR-007 section 1).

Keyed (ADR-004): without FIRMS_MAP_KEY no request is made and health is needs_key. The key sits in
the URL path, so errors are never echoed (see poll.py).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from heimdall.config import AOI, Settings, secret
from heimdall.geo import bbox_for_radius, h3_cell
from heimdall.ingest.poll import PollConnector, Unit, num
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

DEFAULT_PRODUCTS = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT")
_LETTERS = {"l": "low", "n": "nominal", "h": "high"}


def normalise_confidence(v: str) -> str | None:
    """VIIRS l/n/h or MODIS 0-100 -> low (<30) / nominal (<80) / high."""
    s = v.strip().lower()
    if s in _LETTERS:
        return _LETTERS[s]
    try:
        n = float(s)
    except ValueError:
        return None
    if not 0 <= n <= 100:
        return None
    return "low" if n < 30 else "nominal" if n < 80 else "high"


def _row_to_event(r: Mapping[str, str]) -> GeoEvent:
    lat, lon = float(r["latitude"]), float(r["longitude"])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("position out of range")
    frp = float(r["frp"])
    if not 0 <= frp < 1e5:  # also rejects NaN
        raise ValueError("implausible frp")
    hhmm = r["acq_time"].strip().zfill(4)
    ts = datetime.strptime(f"{r['acq_date'].strip()} {hhmm}", "%Y-%m-%d %H%M").replace(tzinfo=UTC)
    conf = normalise_confidence(r["confidence"])
    if conf is None:
        raise ValueError("bad confidence")
    sat, instrument = r["satellite"].strip(), r["instrument"].strip()
    modis = "bright_ti4" not in r  # MODIS: brightness / bright_t31 instead of ti4 / ti5
    return GeoEvent(
        id=f"firms|{sat}|{ts.strftime('%Y%m%dT%H%M')}|{lat:.4f}|{lon:.4f}",
        ts=ts,
        source="firms",
        domain=Domain.SPACE,
        kind="thermal",
        lat=lat,
        lon=lon,
        title="thermal anomaly",
        value=frp,
        attrs={
            "role": "signal",
            "satellite": sat,
            "instrument": instrument,
            "confidence": conf,
            "bright_ti4": num(float(r["brightness" if modis else "bright_ti4"])),
            "bright_ti5": num(float(r["bright_t31" if modis else "bright_ti5"])),
            "scan": num(float(r["scan"])),
            "track": num(float(r["track"])),
            "daynight": r["daynight"].strip().upper()[:1],
            "version": r["version"].strip()[:16],
        },
        h3_r4=h3_cell(lat, lon, 4),
        h3_r6=h3_cell(lat, lon, 6),
    )


def parse_firms_csv(text: str) -> tuple[list[GeoEvent], int]:
    """(events, n_dropped). Duplicate rows collapse to one event; bad rows are dropped+counted."""
    out: dict[str, GeoEvent] = {}
    dropped = 0
    for row in csv.DictReader(io.StringIO(text)):
        try:
            ev = _row_to_event(row)
        except (ValueError, KeyError, TypeError, AttributeError):
            dropped += 1
            continue
        out.setdefault(ev.id, ev)
    return list(out.values()), dropped


class FirmsConnector(PollConnector):
    domain = Domain.SPACE
    token_name = "FIRMS_MAP_KEY"

    def __init__(
        self,
        name: str,
        base_url: str,
        aois: Sequence[AOI],
        token: str | None,
        *,
        products: Sequence[str] = DEFAULT_PRODUCTS,
        day_range: int = 1,
        **kw: Any,
    ) -> None:
        super().__init__(name, token=token, **kw)
        self.base_url = base_url.rstrip("/")
        self.aois = list(aois)
        self.products = list(products)
        self.day_range = max(1, min(5, day_range))

    def units(self) -> list[Unit]:
        return [
            self._unit(aoi, box, p)
            for aoi in self.aois
            for box in bbox_for_radius(aoi.lat, aoi.lon, aoi.radius_nm)
            for p in self.products
        ]

    def _unit(self, aoi: AOI, box: tuple[float, float, float, float], product: str) -> Unit:
        s, w, n, e = box

        async def run() -> tuple[list[GeoEvent], int]:
            url = (
                f"{self.base_url}/area/csv/{self._token}/{product}/"
                f"{w:.3f},{s:.3f},{e:.3f},{n:.3f}/{self.day_range}"
            )
            resp = await self.client.get(url)
            events, dropped = parse_firms_csv(resp.text)
            kept = [
                x
                for x in events
                if x.lat is not None and x.lon is not None and aoi.contains(x.lat, x.lon)
            ]
            return kept, dropped

        return run


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return FirmsConnector(
        key,
        str(cfg["base"]),
        settings.aois,
        secret(str(cfg.get("auth", "FIRMS_MAP_KEY"))),
        products=list(cfg.get("products", DEFAULT_PRODUCTS)),
        day_range=int(cfg.get("day_range", 1)),
        poll_s=float(cfg.get("poll_s", 900)),
        rps=float(cfg.get("rps", 0.5)),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 10800)),
    )
