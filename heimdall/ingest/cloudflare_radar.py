"""Cloudflare Radar outage annotations -> `outage_annotation` GeoEvents (ADR-007 section 1).

Keyed (ADR-004): without CLOUDFLARE_RADAR_TOKEN no request is made and health is needs_key.
Free-text description/cause is UNTRUSTED and goes to `text` only. `title` is built from a validated
country code; `attrs` hold only values matching a strict single-word pattern.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from heimdall.config import Settings, secret
from heimdall.geo import h3_cell
from heimdall.ingest.poll import PollConnector, Unit, num, parse_dt
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

_CODE = re.compile(r"^[A-Z]{2}$")
_WORD = re.compile(r"^[A-Za-z_]{1,24}$")  # enum-like; a sentence cannot pass


def _word(v: Any) -> str | None:
    return v if isinstance(v, str) and _WORD.match(v) else None


def _text(v: Any, n: int = 500) -> str | None:
    return " ".join(v.split())[:n] or None if isinstance(v, str) else None


def parse_annotations(
    payload: Mapping[str, Any],
    centroids: Mapping[str, tuple[float, float]] | None = None,
) -> tuple[list[GeoEvent], int]:
    """(events, n_dropped). `centroids` maps country code -> (lat, lon) for optional placement."""
    result = payload.get("result")
    anns = result.get("annotations") if isinstance(result, Mapping) else None
    events: list[GeoEvent] = []
    dropped = 0
    for a in anns if isinstance(anns, list) else []:
        try:
            events.append(_annotation(a, centroids or {}))
        except (ValueError, TypeError, AttributeError):
            dropped += 1
    return events, dropped


def _annotation(a: Any, centroids: Mapping[str, tuple[float, float]]) -> GeoEvent:
    if not isinstance(a, Mapping):
        raise ValueError("not an object")
    aid = a.get("id")
    start, end = parse_dt(a.get("startDate")), parse_dt(a.get("endDate"))
    if not isinstance(aid, str) or not re.match(r"^[\w.:-]{1,80}$", aid) or start is None:
        raise ValueError("no id/start")
    if end is not None and end < start:
        raise ValueError("end before start")
    locs = a.get("locations")
    loc = (
        next((x for x in locs if isinstance(x, str) and _CODE.match(x)), None)
        if isinstance(locs, list)
        else None
    )
    asns = a.get("asns")
    asn = (
        next((int(x) for x in asns if num(x) is not None), None) if isinstance(asns, list) else None
    )
    out = a.get("outage")
    out = out if isinstance(out, Mapping) else {}
    lat, lon = centroids.get(loc, (None, None)) if loc else (None, None)
    cause = _text(out.get("outageCause"), 60)
    desc = _text(a.get("description"))
    text = " | ".join(x for x in (desc, cause) if x) or None
    return GeoEvent(
        id=f"cloudflare_radar|{aid}",
        ts=start,
        source="cloudflare_radar",
        domain=Domain.CYBER,
        kind="outage_annotation",
        lat=lat,
        lon=lon,
        valid_from=start,
        valid_to=end,
        title=f"Internet outage {loc}" if loc else "Internet outage",
        text=text,  # UNTRUSTED
        value=None,
        attrs={
            "role": "signal",
            "location": loc,
            "asn": asn,
            "scope": _word(a.get("scope")),
            "outage_type": _word(out.get("outageType")),
        },
        h3_r4=h3_cell(lat, lon, 4) if lat is not None and lon is not None else "",
        h3_r6=h3_cell(lat, lon, 6) if lat is not None and lon is not None else "",
    )


class CloudflareRadarConnector(PollConnector):
    domain = Domain.CYBER
    token_name = "CLOUDFLARE_RADAR_TOKEN"

    def __init__(
        self,
        name: str,
        base_url: str,
        token: str | None,
        *,
        centroids: Mapping[str, tuple[float, float]] | None = None,
        date_range: str = "7d",
        **kw: Any,
    ) -> None:
        super().__init__(name, token=token, **kw)
        self.base_url = base_url.rstrip("/")
        self.centroids = dict(centroids or {})
        self.date_range = date_range

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            resp = await self.client.get(
                f"{self.base_url}/annotations/outages",
                params={"dateRange": self.date_range, "limit": 100, "format": "json"},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            return parse_annotations(resp.json(), self.centroids)

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    from heimdall.ingest.ioda import load_entities

    centroids = {e.code: (e.lat, e.lon) for e in load_entities() if e.type == "country"}
    return CloudflareRadarConnector(
        key,
        str(cfg["base"]),
        secret(str(cfg.get("auth", "CLOUDFLARE_RADAR_TOKEN"))),
        centroids=centroids,
        poll_s=float(cfg.get("poll_s", 900)),
        rps=float(cfg.get("rps", 0.5)),
        reliability=SourceReliability(cfg.get("reliability", "B")),
        expected_latency_s=int(cfg.get("latency_s", 900)),
    )
