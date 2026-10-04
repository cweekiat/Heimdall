"""PSK Reporter reception reports (REST, no key) -> `hf_spots` GeoEvents (ADR-008, band HF).

The query API asks for at most one request per 5 minutes, so `poll_s` defaults to 300 and each
poll asks only for reports newer than the last sequence number (`lastseqno`). The live MQTT feed
is not used: its ports are not reachable through this machine's egress proxy (checked 2026-10-01).

Reports are aggregated like WSPR (per 10-min bin, band, great-circle midpoint cell). Because a bin
is filled across several polls, open bins are accumulated in memory and re-emitted with the same
id (the store upserts by id). The XML is untrusted: bodies with a DOCTYPE (entity expansion) or
over `max_bytes` are rejected, and callsigns are never stored, only counts.
"""

from __future__ import annotations

import statistics
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from heimdall.config import Settings
from heimdall.ingest.hf_common import (
    band_of,
    bin_start,
    distance_km,
    grid,
    in_bbox,
    maidenhead,
    midpoint,
    spot_id,
)
from heimdall.ingest.poll import PollConnector, Unit
from heimdall.ingest.wspr import DEFAULT_BBOX, _bbox
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

MAX_BYTES = 20_000_000
MAX_SNR_SAMPLES = 2000  # per bin; median of a bounded sample


@dataclass
class Report:
    ts: datetime
    band: int
    mode: str
    snr: float | None
    tx: tuple[float, float]
    rx: tuple[float, float]
    tx_key: str  # hashed-free identity for distinct counts (locator, not callsign)
    rx_key: str


def parse_reports(
    body: bytes | str, *, max_bytes: int = MAX_BYTES
) -> tuple[list[Report], int, int]:
    """(reports, n_dropped, last_sequence_number). Unsafe or malformed body -> ([], 0, 0)."""
    raw = body.encode() if isinstance(body, str) else body
    if len(raw) > max_bytes or b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw:
        raise ValueError("unsafe or oversized XML")
    root = ET.fromstring(raw)
    seq = 0
    last = root.find("lastSequenceNumber")
    if last is not None:
        try:
            seq = int(last.get("value", "0"))
        except ValueError:
            seq = 0
    out: list[Report] = []
    dropped = 0
    for el in root.iter("receptionReport"):
        a = el.attrib
        try:
            ts = datetime.fromtimestamp(int(a.get("flowStartSeconds", "")), UTC)
            freq = float(a.get("frequency", ""))
        except ValueError:
            dropped += 1
            continue
        band = band_of(freq)
        tx, rx = maidenhead(a.get("senderLocator")), maidenhead(a.get("receiverLocator"))
        if band is None or tx is None or rx is None:
            dropped += 1
            continue
        try:
            snr: float | None = float(a["sNR"])
        except (KeyError, ValueError):
            snr = None
        out.append(
            Report(
                ts=ts,
                band=band,
                mode=a.get("mode", "")[:12],
                snr=snr,
                tx=tx,
                rx=rx,
                tx_key=a.get("senderLocator", "")[:6].upper(),
                rx_key=a.get("receiverLocator", "")[:6].upper(),
            )
        )
    return out, dropped, seq


@dataclass
class _Bin:
    t: datetime
    band: int
    glat: float
    glon: float
    n: int = 0
    dist_sum: float = 0.0
    snrs: list[float] = field(default_factory=list)
    paths: set[tuple[str, str]] = field(default_factory=set)
    rx: set[str] = field(default_factory=set)
    tx: set[str] = field(default_factory=set)
    modes: dict[str, int] = field(default_factory=dict)
    receiver: bool = False  # task 060: receiver-cell bin (`hf_rx`) instead of path midpoint


class Aggregator:
    """Open bins keyed by stable id; `add` returns the ids touched."""

    def __init__(
        self,
        *,
        bin_min: int = 10,
        grid_deg: float = 2.0,
        bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
        min_km: float = 500,
        max_km: float = 12000,
        keep_h: float = 3,
        rx_grid_deg: float = 0.0,
    ) -> None:
        self.bin_min, self.grid_deg, self.bbox = bin_min, grid_deg, bbox
        self.rx_grid_deg = rx_grid_deg  # 0 = no receiver-cell bins
        self.min_km, self.max_km, self.keep = min_km, max_km, timedelta(hours=keep_h)
        self.bins: dict[str, _Bin] = {}

    def add(self, reports: Sequence[Report]) -> tuple[set[str], int]:
        touched: set[str] = set()
        skipped = 0
        for r in reports:
            d = distance_km(*r.tx, *r.rx)
            if not self.min_km <= d <= self.max_km:
                skipped += 1
                continue
            t = bin_start(r.ts, self.bin_min)
            mlat, mlon = midpoint(*r.tx, *r.rx)
            glat, glon = grid(mlat, self.grid_deg), grid(mlon, self.grid_deg)
            if in_bbox(glat, glon, self.bbox):
                touched.add(self._put(spot_id("psk", t, r.band, glat, glon), r, t, glat, glon, d))
            else:
                skipped += 1
            if self.rx_grid_deg > 0:
                rlat, rlon = grid(r.rx[0], self.rx_grid_deg), grid(r.rx[1], self.rx_grid_deg)
                if in_bbox(r.rx[0], r.rx[1], self.bbox):
                    key = spot_id("pskrx", t, r.band, rlat, rlon)
                    touched.add(self._put(key, r, t, rlat, rlon, d, receiver=True))
        return touched, skipped

    def _put(
        self,
        key: str,
        r: Report,
        t: datetime,
        glat: float,
        glon: float,
        d: float,
        receiver: bool = False,
    ) -> str:
        b = self.bins.get(key)
        if b is None:
            b = self.bins[key] = _Bin(t, r.band, glat, glon, receiver=receiver)
        b.n += 1
        b.dist_sum += d
        if r.snr is not None and len(b.snrs) < MAX_SNR_SAMPLES:
            b.snrs.append(r.snr)
        b.paths.add((r.tx_key, r.rx_key))
        b.rx.add(r.rx_key)
        b.tx.add(r.tx_key)
        b.modes[r.mode] = b.modes.get(r.mode, 0) + 1
        return key

    def expire(self, now: datetime) -> None:
        cut = now - self.keep
        for k in [k for k, b in self.bins.items() if b.t < cut]:
            del self.bins[k]

    def event(self, key: str) -> GeoEvent:
        b = self.bins[key]
        return GeoEvent(
            id=key,
            ts=b.t,
            source="pskreporter",
            domain=Domain.SPECTRUM,
            kind="hf_rx" if b.receiver else "hf_spots",
            lat=b.glat,
            lon=b.glon,
            valid_from=b.t,
            valid_to=b.t + timedelta(minutes=self.bin_min),
            title=f"PSK Reporter {b.band} MHz {'reception' if b.receiver else 'spots'}",
            value=float(b.n),
            attrs={
                "role": "signal",
                "band_mhz": b.band,
                "mode": max(b.modes, key=lambda m: b.modes[m]) if b.modes else "",
                "n": b.n,
                "paths": len(b.paths),
                "n_rx": len(b.rx),
                "n_tx": len(b.tx),
                "snr_med": statistics.median(b.snrs) if b.snrs else None,
                "snr_mean": statistics.fmean(b.snrs) if b.snrs else None,
                "mean_dist_km": b.dist_sum / b.n,
                "grid_deg": self.rx_grid_deg if b.receiver else self.grid_deg,
                "geo": "receiver_cell" if b.receiver else "path_midpoint",
            },
        )


class PskReporterConnector(PollConnector):
    domain = Domain.SPECTRUM

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        window_s: int = 900,
        app_contact: str = "heimdall-osint-research",
        agg: Aggregator | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.base_url = base_url.rstrip("/")
        self.window_s, self.app_contact = window_s, app_contact
        self.agg = agg or Aggregator()
        self.last_seq = 0

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            params: dict[str, str | int] = {
                "flowStartSeconds": -self.window_s,
                "rronly": 1,
                "noactive": 1,
                "appcontact": self.app_contact,
            }
            if self.last_seq:
                params["lastseqno"] = self.last_seq
            r = await self.client.get(f"{self.base_url}/query", params=params)
            reports, dropped, seq = parse_reports(r.content)
            if seq:
                self.last_seq = seq
            touched, skipped = self.agg.add(reports)
            self.agg.expire(self._now())
            return [self.agg.event(k) for k in touched if k in self.agg.bins], dropped + skipped

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    agg = Aggregator(
        bin_min=int(cfg.get("bin_min", 10)),
        grid_deg=float(cfg.get("grid_deg", 2.0)),
        bbox=_bbox(cfg.get("bbox")),
        min_km=float(cfg.get("min_km", 500)),
        max_km=float(cfg.get("max_km", 12000)),
        rx_grid_deg=float(cfg.get("rx_grid_deg", 0.0)),
    )
    return PskReporterConnector(
        key,
        str(cfg["base"]),
        window_s=int(cfg.get("window_s", 900)),
        agg=agg,
        poll_s=max(300.0, float(cfg.get("poll_s", 300))),  # provider asks for >= 5 min
        rps=float(cfg.get("rps", 0.1)),
        reliability=SourceReliability(cfg.get("reliability", "C")),
        expected_latency_s=int(cfg.get("latency_s", 900)),
    )
