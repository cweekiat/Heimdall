"""Build enabled connectors from config/sources.yaml + config/config.yaml."""

from __future__ import annotations

from typing import Any

from heimdall.config import Settings, sources
from heimdall.ingest.adsb import AdsbAggregatorConnector
from heimdall.ingest.http import RateLimitedClient
from heimdall.schemas import SourceReliability

# source key -> (provider name stored on rows, path template)
ADSB_PROVIDERS: dict[str, tuple[str, str]] = {
    "adsb_lol": ("adsb.lol", "/v2/point/{lat}/{lon}/{radius}"),
    "adsb_fi": ("adsb.fi", "/v2/lat/{lat}/lon/{lon}/dist/{radius}"),
    "airplanes_live": ("airplanes.live", "/v2/point/{lat}/{lon}/{radius}"),
}


def build_connectors(
    settings: Settings, src: dict[str, Any] | None = None, enabled: set[str] | None = None
) -> list[AdsbAggregatorConnector]:
    cfg = (src if src is not None else sources()).get("sources", {})
    out: list[AdsbAggregatorConnector] = []
    for key, (provider, template) in ADSB_PROVIDERS.items():
        if key not in cfg or (enabled is not None and key not in enabled):
            continue
        c = cfg[key]
        rps = float(c.get("rps", 1))
        if rps <= 0:
            raise ValueError(f"sources.{key}.rps must be > 0, got {rps}")
        out.append(
            AdsbAggregatorConnector(
                key,
                c["base"],
                template,
                settings.aois,
                provider=provider,
                rps=rps,
                reliability=SourceReliability(c.get("reliability", "B")),
                expected_latency_s=int(c.get("latency_s", 5)),
                client=RateLimitedClient(key, rps=rps),
            )
        )
    return out
