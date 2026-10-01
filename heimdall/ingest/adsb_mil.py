"""Worldwide military aircraft: readsb `/v2/mil` (task 039).

adsb.lol and adsb.fi expose every aircraft whose ICAO address their database flags military
(dbFlags & 1): ~250-300 worldwide at a time, ~110 KB per call. Polled every `poll_s` from the first
provider, falling back to the next on failure. Rows are `is_global=True`: they feed the live map,
the military track store and the military detectors, never the per-area GNSS detectors (those use
the area connectors, which see the same aircraft with full NIC/NACp context).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Any

import httpx

from heimdall.config import Settings
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.ingest.poll import PollConnector
from heimdall.ingest.readsb import parse_readsb_v2
from heimdall.ports import Connector
from heimdall.schemas import AircraftObs, Domain, SourceReliability

DEFAULT_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("adsb.lol", "https://api.adsb.lol/v2/mil"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/mil"),
)


class AdsbMilConnector(PollConnector):
    domain = Domain.AIR

    def __init__(
        self,
        name: str,
        providers: Sequence[tuple[str, str]] = DEFAULT_PROVIDERS,
        *,
        clients: Sequence[RateLimitedClient] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.providers = list(providers)
        self._clients = list(clients) if clients else None
        self.provider_used: str | None = None

    def _client_for(self, i: int) -> RateLimitedClient:
        if self._clients is None:
            self._clients = [
                RateLimitedClient(f"{self.name}:{p}", rps=self._rps) for p, _ in self.providers
            ]
        return self._clients[i]

    async def poll_once(self) -> list[AircraftObs]:  # type: ignore[override]
        for i, (provider, url) in enumerate(self.providers):
            try:
                r = await self._client_for(i).get(url)
                payload = r.json()
            except (httpx.HTTPError, CircuitOpenError, ValueError) as e:
                status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                self._fail(f"{provider}: " + (f"HTTP {status}" if status else type(e).__name__))
                continue
            if not isinstance(payload, Mapping):
                self._fail(f"{provider}: not an object")
                continue
            obs, dropped = parse_readsb_v2(dict(payload), provider, self._now().astimezone(UTC))
            obs = [o.model_copy(update={"is_global": True, "mil": True}) for o in obs]
            self.n_ok += 1
            self.n_dropped += dropped
            self.last_ok = self._now()
            self.provider_used = provider
            self.records_hour.add(len(obs))
            return obs
        return []

    async def aclose(self) -> None:
        self._stop = True
        for c in self._clients or []:
            await c.aclose()


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    raw = cfg.get("providers")
    providers = (
        [(str(p["name"]), str(p["url"])) for p in raw if isinstance(p, Mapping)]
        if isinstance(raw, list)
        else list(DEFAULT_PROVIDERS)
    )
    return AdsbMilConnector(
        key,
        providers,
        poll_s=float(cfg.get("poll_s", 60)),
        rps=float(cfg.get("rps", 0.2)),
        reliability=SourceReliability(cfg.get("reliability", "B")),
        expected_latency_s=int(cfg.get("latency_s", 60)),
    )
