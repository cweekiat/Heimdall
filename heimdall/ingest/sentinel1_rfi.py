"""Sentinel-1 RFI annotations (Planetary Computer) -> `sar_rfi` GeoEvents (ADR-008, C-band).

Since IPF 3.4 every Sentinel-1 Level-1 product carries an RFI annotation file (`annotation/rfi/`):
per-swath RFI detection from noise-sensing pulses (`rfiDetected`, KL divergence, Fisher Z, RFI PSD)
and per-burst in-band/out-of-band power ratios. Planetary Computer exposes it as the item asset
`schema-product-<pol>` (root element `<rfi>`, ~12 KB). A ground C-band emitter illuminating the
satellite during a pass raises these values along that stretch of the swath.

METADATA ONLY (user decision 2026-09-30: no Sentinel imagery/COG/thumbnail downloads). Only the RFI
XML assets are fetched, only from `allowed_hosts`, signed with Planetary Computer's free anonymous
SAS token. Bodies with a DOCTYPE/ENTITY or over `max_bytes` are rejected (untrusted XML).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from heimdall.config import AOI, Settings
from heimdall.geo import bbox_for_radius
from heimdall.ingest.poll import PollConnector, Unit, parse_dt
from heimdall.ingest.stac import _centroid
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

MAX_BYTES = 2_000_000
DEFAULT_ASSETS = (
    "schema-product-vv",
    "schema-product-vh",
    "schema-product-hh",
    "schema-product-hv",
)
DEFAULT_HOSTS = (".blob.core.windows.net",)


def _f(v: str | None) -> float | None:
    try:
        x = float(v) if v is not None else None
    except ValueError:
        return None
    return x if x is not None and x == x and abs(x) != float("inf") else None


def parse_rfi(body: bytes | str, *, max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """RFI annotation XML -> summary numbers. Raises ValueError if unsafe or not an RFI file."""
    raw = body.encode() if isinstance(body, str) else body
    if len(raw) > max_bytes or b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw:
        raise ValueError("unsafe or oversized XML")
    root = ET.fromstring(raw)
    if root.tag != "rfi":
        raise ValueError("not an RFI annotation")
    pol = (root.findtext("adsHeader/polarisation") or "").strip().upper()[:2]
    noise = list(root.iter("rfiDetectionFromNoiseReport"))
    flagged = [r for r in noise if (r.findtext("rfiDetected") or "").strip().lower() == "true"]
    psd = [x for x in (_f(r.findtext("maxRfiPsd")) for r in noise) if x is not None]
    kl = [x for x in (_f(r.findtext("maxKLDivergence")) for r in noise) if x is not None]
    fz = [x for x in (_f(r.findtext("maxFisherZ")) for r in noise) if x is not None]
    bursts = list(root.iter("rfiBurstReport"))
    ratio = [
        x for x in (_f(b.findtext("inBandOutBandPowerRatio")) for b in bursts) if x is not None
    ]
    burst_flag = sum(
        1
        for b in bursts
        if any((e.text or "").strip().lower() == "true" for e in b.iter() if "Detected" in e.tag)
    )
    swaths = sorted({(r.findtext("swath") or "").strip()[:4] for r in flagged} - {""})
    return {
        "pol": pol,
        "mitigation_applied": (root.findtext("rfiMitigationApplied") or "").strip()[:20] or None,
        "n_noise_reports": len(noise),
        "n_noise_rfi": len(flagged),
        "noise_rfi_frac": len(flagged) / len(noise) if noise else None,
        "max_rfi_psd": max(psd) if psd else None,
        "max_kl_divergence": max(kl) if kl else None,
        "max_fisher_z": max(fz) if fz else None,
        "n_bursts": len(bursts),
        "n_bursts_rfi": burst_flag,
        "max_power_ratio": max(ratio) if ratio else None,
        "mean_power_ratio": sum(ratio) / len(ratio) if ratio else None,
        "rfi_swaths": swaths,
        "start": parse_dt((root.findtext("adsHeader/startTime") or "").strip()),
        "stop": parse_dt((root.findtext("adsHeader/stopTime") or "").strip()),
    }


def rfi_assets(
    item: Mapping[str, Any],
    keys: Sequence[str] = DEFAULT_ASSETS,
    hosts: Sequence[str] = DEFAULT_HOSTS,
) -> dict[str, str]:
    """Asset key -> https href for RFI XML assets on an allowed host (nothing else is fetched)."""
    assets = item.get("assets")
    out: dict[str, str] = {}
    if not isinstance(assets, Mapping):
        return out
    for k in keys:
        a = assets.get(k)
        if not isinstance(a, Mapping) or not isinstance(a.get("href"), str):
            continue
        href: str = a["href"]
        u = urlsplit(href)
        host_ok = u.scheme == "https" and any((u.hostname or "").endswith(h) for h in hosts)
        if host_ok and a.get("type", "application/xml") == "application/xml":
            out[k] = href.split("?", 1)[0]
    return out


def to_event(item: Mapping[str, Any], pols: Sequence[Mapping[str, Any]]) -> GeoEvent:
    """One scene + its per-polarisation RFI summaries -> one `sar_rfi` GeoEvent."""
    iid = item.get("id")
    geom = item.get("geometry")
    props = item.get("properties") if isinstance(item.get("properties"), Mapping) else {}
    assert isinstance(props, Mapping)
    ts = parse_dt(props.get("datetime"))
    if not isinstance(iid, str) or not isinstance(geom, Mapping) or ts is None or not pols:
        raise ValueError("incomplete item")
    lat, lon = _centroid(geom)
    fracs = [p["noise_rfi_frac"] for p in pols if p.get("noise_rfi_frac") is not None]
    ratios = [p["max_power_ratio"] for p in pols if p.get("max_power_ratio") is not None]
    detected = any(p["n_noise_rfi"] or p["n_bursts_rfi"] for p in pols)
    starts = [p["start"] for p in pols if p.get("start")]
    stops = [p["stop"] for p in pols if p.get("stop")]
    return GeoEvent(
        id=f"s1rfi|{iid[:120]}",
        ts=ts,
        source="sentinel1_rfi",
        domain=Domain.SPECTRUM,
        kind="sar_rfi",
        lat=lat,
        lon=lon,
        geometry=dict(geom),
        valid_from=min(starts).replace(tzinfo=UTC) if starts else None,
        valid_to=max(stops).replace(tzinfo=UTC) if stops else None,
        title="Sentinel-1 RFI report" + (" (RFI detected)" if detected else ""),
        value=max(fracs) if fracs else None,
        attrs={
            "role": "signal",
            "stac_item_id": iid[:120],
            "platform": str(props.get("platform", ""))[:20] or None,
            "orbit_state": str(props.get("sat:orbit_state", ""))[:12] or None,
            "relative_orbit": props.get("sat:relative_orbit")
            if isinstance(props.get("sat:relative_orbit"), int)
            else None,
            "rfi_detected": detected,
            "max_power_ratio": max(ratios) if ratios else None,
            "pols": {
                p["pol"] or f"p{i}": {k: v for k, v in p.items() if k not in ("start", "stop")}
                for i, p in enumerate(pols)
            },
        },
    )


class Sentinel1RfiConnector(PollConnector):
    domain = Domain.SPECTRUM

    def __init__(
        self,
        name: str,
        base_url: str,
        aois: Sequence[AOI],
        *,
        sas_url: str,
        collection: str = "sentinel-1-grd",
        lookback_days: float = 3,
        max_items: int = 200,
        asset_keys: Sequence[str] = DEFAULT_ASSETS,
        allowed_hosts: Sequence[str] = DEFAULT_HOSTS,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.base_url = base_url.rstrip("/")
        self.aois = list(aois)
        self.sas_url, self.collection = sas_url, collection
        self.lookback_days, self.max_items = lookback_days, max_items
        self.asset_keys, self.allowed_hosts = tuple(asset_keys), tuple(allowed_hosts)
        self.done: set[str] = set()
        self._sas: tuple[str, datetime] | None = None

    async def _sas_token(self) -> str:
        now = self._now()
        if self._sas and self._sas[1] - now > timedelta(minutes=5):
            return self._sas[0]
        d = (await self.client.get(self.sas_url)).json()
        tok, exp = d.get("token"), parse_dt(d.get("msft:expiry"))
        if not isinstance(tok, str) or not tok:
            raise ValueError("no SAS token")
        self._sas = (tok, exp or now + timedelta(minutes=30))
        return tok

    async def _search(self, bbox: tuple[float, float, float, float]) -> list[Mapping[str, Any]]:
        now = self._now().astimezone(UTC)
        dt = f"{(now - timedelta(days=self.lookback_days)).strftime('%Y-%m-%dT%H:%M:%SZ')}/.."
        s, w, n, e = bbox
        r = await self.client.get(
            f"{self.base_url}/collections/{self.collection}/items",
            params={"bbox": f"{w},{s},{e},{n}", "datetime": dt, "limit": self.max_items},
        )
        feats = r.json().get("features")
        return [f for f in feats if isinstance(f, Mapping)] if isinstance(feats, list) else []

    async def _scene(self, item: Mapping[str, Any]) -> GeoEvent | None:
        hrefs = rfi_assets(item, self.asset_keys, self.allowed_hosts)
        if not hrefs:
            return None
        tok = await self._sas_token()
        pols = []
        for href in hrefs.values():
            r = await self.client.get(f"{href}?{tok}")
            try:
                pols.append(parse_rfi(r.content))
            except (ValueError, ET.ParseError):
                continue
        return to_event(item, pols) if pols else None

    def units(self) -> Sequence[Unit]:
        async def run(aoi: AOI) -> tuple[list[GeoEvent], int]:
            events: list[GeoEvent] = []
            dropped = 0
            for bbox in bbox_for_radius(aoi.lat, aoi.lon, aoi.radius_nm):
                for item in await self._search(bbox):
                    iid = item.get("id")
                    if not isinstance(iid, str) or iid in self.done:
                        continue
                    try:
                        ev = await self._scene(item)
                    except ValueError:
                        dropped += 1
                        continue
                    self.done.add(iid)
                    if ev is not None:
                        events.append(ev)
            return events, dropped

        def unit(a: AOI) -> Unit:
            return lambda: run(a)

        return [unit(a) for a in self.aois]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return Sentinel1RfiConnector(
        key,
        str(cfg["base"]),
        settings.aois,
        sas_url=str(cfg["sas_url"]),
        collection=str(cfg.get("collection", "sentinel-1-grd")),
        lookback_days=float(cfg.get("lookback_days", 3)),
        max_items=int(cfg.get("max_items", 200)),
        asset_keys=tuple(cfg.get("asset_keys", DEFAULT_ASSETS)),
        allowed_hosts=tuple(cfg.get("allowed_hosts", DEFAULT_HOSTS)),
        poll_s=float(cfg.get("poll_s", 21600)),
        rps=float(cfg.get("rps", 0.5)),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 86400)),
    )
