"""NOAA SWPC planetary K-index + NOAA scales -> `space_weather` confounder GeoEvents (ADR-007).

No key, no position (lat/lon None). One event per Kp interval. NOAA scales are a current snapshot,
so r/s/g scales from it are attached to the latest Kp interval only; earlier intervals carry a
G-scale derived from Kp alone (Kp 5 -> G1 ... Kp 9 -> G5, NOAA's definition) and null r/s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from heimdall.config import Settings
from heimdall.ingest.poll import PollConnector, Unit, parse_dt
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability


def _kp_rows(payload: Any) -> list[tuple[Any, Any]]:
    """(time_tag, kp) pairs from either the dict-list or the header+rows-list JSON layout."""
    if not isinstance(payload, list) or not payload:
        return []
    if isinstance(payload[0], Mapping):
        return [(r.get("time_tag"), r.get("Kp", r.get("kp_index"))) for r in payload]
    head = payload[0]
    if not isinstance(head, list) or "time_tag" not in head:
        return []
    ti = head.index("time_tag")
    ki = head.index("Kp") if "Kp" in head else head.index("kp_index") if "kp_index" in head else -1
    return [(r[ti], r[ki]) for r in payload[1:] if isinstance(r, list) and 0 <= ki < len(r)]


def _scale(v: Any) -> int | None:
    """NOAA scale entry {'Scale': '3', 'Text': ...} -> 3 (0..5)."""
    raw = v.get("Scale") if isinstance(v, Mapping) else None
    try:
        n = int(str(raw))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 5 else None


def parse_swpc(kp_payload: Any, scales_payload: Any = None) -> tuple[list[GeoEvent], int]:
    """(events, n_dropped). Scales (optional) enrich the latest Kp interval."""
    events: list[GeoEvent] = []
    dropped = 0
    for tag, kp_raw in _kp_rows(kp_payload):
        ts = parse_dt(tag)
        try:
            kp = float(kp_raw)
        except (TypeError, ValueError):
            kp = None
        if ts is None or kp is None or not 0 <= kp <= 9:  # also rejects NaN
            dropped += 1
            continue
        events.append(
            GeoEvent(
                id=f"swpc|kp|{ts.strftime('%Y%m%dT%H%M')}",
                ts=ts,
                source="swpc",
                domain=Domain.SPACE,
                kind="space_weather",
                title="planetary K-index",
                value=kp,
                attrs={
                    "role": "confounder",
                    "kp": kp,
                    "g_scale": max(0, int(kp + 1e-9) - 4),
                    "r_scale": None,
                    "s_scale": None,
                    "product": "planetary_k_index",
                },
            )
        )
    if events and isinstance(scales_payload, Mapping):
        cur = scales_payload.get("0")
        if isinstance(cur, Mapping):
            latest = max(events, key=lambda e: e.ts)
            g, r, s = _scale(cur.get("G")), _scale(cur.get("R")), _scale(cur.get("S"))
            if g is not None:
                latest.attrs["g_scale"] = g
            latest.attrs["r_scale"], latest.attrs["s_scale"] = r, s
            latest.attrs["product"] = "planetary_k_index+noaa_scales"
    return events, dropped


class SwpcConnector(PollConnector):
    domain = Domain.SPACE

    def __init__(self, name: str, base_url: str, **kw: Any) -> None:
        super().__init__(name, **kw)
        self.base_url = base_url.rstrip("/")

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            kp = await self.client.get(f"{self.base_url}/products/noaa-planetary-k-index.json")
            scales: Any = None
            try:  # scales are an enrichment; Kp alone is still useful
                scales = (
                    await self.client.get(f"{self.base_url}/products/noaa-scales.json")
                ).json()
            except Exception as e:
                self._fail(type(e).__name__)
            return parse_swpc(kp.json(), scales)

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return SwpcConnector(
        key,
        str(cfg["base"]),
        poll_s=float(cfg.get("poll_s", 1800)),
        rps=float(cfg.get("rps", 0.5)),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 3600)),
    )
