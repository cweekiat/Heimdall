"""Build enabled connectors from config/sources.yaml + config/config.yaml."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from heimdall.config import Settings, sources
from heimdall.ingest.adsb import AdsbAggregatorConnector, sweep_grid
from heimdall.ingest.http import RateLimitedClient
from heimdall.logging import get_logger
from heimdall.ports import Connector
from heimdall.schemas import Domain, SourceHealth, SourceReliability

log = get_logger("heimdall.registry")

# source key -> (provider name stored on rows, path template)
ADSB_PROVIDERS: dict[str, tuple[str, str]] = {
    "adsb_lol": ("adsb.lol", "/v2/point/{lat}/{lon}/{radius}"),
    "adsb_fi": ("adsb.fi", "/v2/lat/{lat}/lon/{lon}/dist/{radius}"),
}


class DisabledConnector:
    """Placeholder for a configured source whose implementation is missing ( item 5)."""

    def __init__(self, name: str, cfg: Mapping[str, Any], detail: str = "not installed") -> None:
        self.name = name
        self.domain = Domain(cfg.get("domain", "events"))
        self.reliability = SourceReliability(cfg.get("reliability", "F"))
        self.expected_latency_s = int(cfg.get("latency_s", 0))
        self.detail = detail

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        return
        yield  # pragma: no cover - makes this an async generator

    def health(self) -> SourceHealth:
        return SourceHealth(
            source=self.name,
            domain=self.domain,
            reliability=self.reliability,
            expected_latency_s=self.expected_latency_s,
            status="disabled",
            detail=self.detail,
        )

    async def aclose(self) -> None:
        return None


def _load_impl(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    impl = str(cfg["impl"])
    try:
        module, _, func = impl.partition(":")
        factory = getattr(importlib.import_module(module), func or "build")
    except (ImportError, AttributeError, ValueError) as e:
        log.info("connector_not_installed", source=key, impl=impl, error=str(e))
        return DisabledConnector(key, cfg)
    try:
        conn: Connector = factory(key, cfg, settings)
    except Exception as e:  # a broken source must not stop the others
        log.error("connector_build_failed", source=key, error=f"{type(e).__name__}: {e}")
        return DisabledConnector(key, cfg, "build failed")
    return conn


def _sweep_kw(sw: Any) -> dict[str, Any]:
    """`gnss_sweep: {enabled, every, spacing_km, lat: [min, max], backoff}` -> connector kwargs."""
    if not isinstance(sw, Mapping) or not sw.get("enabled", False):
        return {}
    lat = sw.get("lat", [-50, 70])
    return {
        "sweep": sweep_grid(float(sw.get("spacing_km", 700)), float(lat[0]), float(lat[1])),
        "sweep_every": int(sw.get("every", 2)),
        "sweep_backoff": int(sw.get("backoff", 12)),
    }


def build_connectors(
    settings: Settings, src: dict[str, Any] | None = None, enabled: set[str] | None = None
) -> list[Connector]:
    cfg = (src if src is not None else sources()).get("sources", {})
    out: list[Connector] = []
    for key, (provider, template) in ADSB_PROVIDERS.items():
        if key not in cfg or (enabled is not None and key not in enabled):
            continue
        c = cfg[key]
        if c.get("enabled", True) is False:  # e.g. provider requires approval we don't have yet
            continue
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
                **_sweep_kw(c.get("gnss_sweep")),
            )
        )
    for key, c in cfg.items():
        if key in ADSB_PROVIDERS or "impl" not in c:
            continue
        if c.get("enabled", True) is False or (enabled is not None and key not in enabled):
            continue
        out.append(_load_impl(key, c, settings))
    return out
