"""CelesTrak GP (OMM JSON) connector: `orbit_elements` plus predicted `overpass` events (ADR-007).

No key. One request per configured satellite per poll (daily); the pass prediction itself is pure
math in heimdall/orbits.py. Satellite list and sensors are config priors (task 016 verifies).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from heimdall.config import AOI, Settings
from heimdall.ingest.adsb import SlidingCounter
from heimdall.ingest.http import CircuitOpenError, RateLimitedClient
from heimdall.logging import DROPPED, RECORDS_IN, get_logger
from heimdall.orbits import OMM_REQUIRED, parse_epoch, passes, satrec_from_omm
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceHealth, SourceReliability

log = get_logger("heimdall.celestrak")

# Only these OMM fields are kept (numbers and short codes); free-text fields are dropped.
OMM_KEEP: tuple[str, ...] = (
    *OMM_REQUIRED,
    "OBJECT_NAME",
    "OBJECT_ID",
    "EPHEMERIS_TYPE",
    "CLASSIFICATION_TYPE",
)
OMM_KEEP += ("ELEMENT_SET_NO", "REV_AT_EPOCH")
_NUMERIC = tuple(k for k in OMM_KEEP if k not in ("EPOCH", "OBJECT_NAME", "OBJECT_ID"))


def _clean_omm(raw: Mapping[str, Any]) -> dict[str, Any]:
    omm: dict[str, Any] = {}
    for k in OMM_KEEP:
        v = raw.get(k)
        if v is None:
            continue
        if k in ("OBJECT_NAME", "OBJECT_ID", "EPOCH", "CLASSIFICATION_TYPE"):
            omm[k] = " ".join(str(v).split())[:40]
        else:
            omm[k] = float(v) if k in _NUMERIC and not isinstance(v, bool) else v
    return omm


def omm_to_event(raw: Mapping[str, Any], name: str | None = None) -> GeoEvent:
    """One OMM record -> `orbit_elements` GeoEvent (validated: SGP4 must initialise from it)."""
    omm = _clean_omm(raw)
    satrec_from_omm(omm)  # raises ValueError if unusable
    epoch = parse_epoch(omm["EPOCH"])
    norad = int(omm["NORAD_CAT_ID"])
    if not 1 <= norad <= 999_999_999 or not 0 <= float(omm["ECCENTRICITY"]) < 1:
        raise ValueError("implausible elements")
    sat_name = name or str(omm.get("OBJECT_NAME") or norad)
    return GeoEvent(
        id=f"celestrak|{norad}|{epoch.isoformat()}",
        ts=epoch,
        source="celestrak",
        domain=Domain.SPACE,
        kind="orbit_elements",
        title=f"{sat_name} elements",
        attrs={
            "role": "context",
            "norad_id": norad,
            "name": sat_name,
            "epoch": epoch.isoformat(),
            "omm": omm,
        },
    )


def parse_gp(payload: Any) -> tuple[list[GeoEvent], int]:
    """(events, n_dropped) for one GP JSON response (a list of OMM dicts)."""
    out: list[GeoEvent] = []
    dropped = 0
    for rec in payload if isinstance(payload, list) else []:
        try:
            out.append(omm_to_event(rec))
        except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
            dropped += 1
    return out, dropped


class CelestrakConnector:
    domain = Domain.SPACE

    def __init__(
        self,
        name: str,
        base_url: str,
        aois: Sequence[AOI],
        satellites: Mapping[int, Mapping[str, Any]],
        *,
        poll_s: float = 86400.0,
        pass_horizon_h: float = 48.0,
        min_elev_deg: float = 20.0,
        rps: float = 0.5,
        reliability: SourceReliability = SourceReliability.A,
        expected_latency_s: int = 86400,
        client: RateLimitedClient | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        groups: Mapping[str, str] | None = None,
        max_per_group: int = 200,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.aois = list(aois)
        self.satellites = {int(k): dict(v) for k, v in satellites.items()}
        self.poll_s, self.pass_horizon_h, self.min_elev_deg = poll_s, pass_horizon_h, min_elev_deg
        self.reliability = reliability
        self.expected_latency_s = expected_latency_s
        self._client = client
        self._rps = rps
        self._sleep, self._now = sleep, now
        self._stop = False
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.n_ok = 0
        self.n_err = 0
        self.n_dropped = 0
        self.records_hour = SlidingCounter()
        # CelesTrak groups for the globe (e.g. gnss): elements only, no pass prediction
        self.groups = dict(groups or {})
        self.max_per_group = max_per_group
        self._omms: list[tuple[dict[str, Any], dict[str, Any]]] = []  # last elements, for passes
        self._wake: asyncio.Event | None = None
        self._dirty = False

    def set_aois(self, aois: Sequence[AOI]) -> None:
        """New areas: recompute passes from the cached elements (CelesTrak asks clients not to
        re-download the same data within 2 h, so an area edit must not trigger a fetch)."""
        self.aois = list(aois)
        self._dirty = True
        if self._wake is not None:
            self._wake.set()

    def _passes(self, start: datetime) -> dict[str, GeoEvent]:
        out: dict[str, GeoEvent] = {}
        for omm, meta in self._omms:
            for aoi in self.aois:
                try:
                    found = passes(
                        omm,
                        (aoi.lat, aoi.lon),
                        start,
                        self.pass_horizon_h,
                        self.min_elev_deg,
                        aoi=aoi.name,
                        sensor=str(meta.get("sensor", "")),
                    )
                except ValueError as e:
                    self._fail(f"pass prediction {omm.get('NORAD_CAT_ID')}: {e}")
                    continue
                out.update((p.id, p) for p in found)
        return out

    async def _poll_groups(self) -> dict[str, GeoEvent]:
        out: dict[str, GeoEvent] = {}
        for code, label in self.groups.items():
            try:
                resp = await self.client.get(
                    f"{self.base_url}/NORAD/elements/gp.php",
                    params={"GROUP": code, "FORMAT": "json"},
                )
                payload = resp.json()
            except httpx.HTTPStatusError as e:
                self._fail(f"group {code}: HTTP {e.response.status_code}")
                continue
            except (httpx.HTTPError, CircuitOpenError, ValueError) as e:
                self._fail(f"group {code}: {type(e).__name__}: {e}")
                continue
            events, dropped = parse_gp(payload)
            self.n_dropped += dropped
            for ev in events[: self.max_per_group]:
                ev.attrs["group"] = str(label)[:40]
                out[ev.id] = ev
            if events:
                self.n_ok += 1
                self.last_ok = self._now()
        return out

    @property
    def client(self) -> RateLimitedClient:
        if self._client is None:
            self._client = RateLimitedClient(self.name, rps=self._rps)
        return self._client

    def _fail(self, text: str) -> None:
        self.n_err += 1
        self.last_error = text[:200]
        self.last_error_at = self._now()
        log.warning("celestrak_fetch_failed", source=self.name, error=self.last_error)

    def health(self) -> SourceHealth:
        total = self.n_ok + self.n_err
        h = SourceHealth(
            source=self.name,
            domain=self.domain,
            reliability=self.reliability,
            last_ok=self.last_ok,
            last_error=self.last_error,
            last_error_at=self.last_error_at,
            records_last_hour=self.records_hour.total(),
            error_rate=self.n_err / total if total else 0.0,
            expected_latency_s=self.expected_latency_s,
        )
        if self.last_error_at is not None and (
            self.last_ok is None or self.last_error_at >= self.last_ok
        ):
            h.status, h.detail = "degraded", self.last_error
        return h

    async def poll_once(self) -> list[GeoEvent]:
        out: dict[str, GeoEvent] = {}
        omms: list[tuple[dict[str, Any], dict[str, Any]]] = []
        start = self._now()
        for norad, meta in self.satellites.items():
            try:
                resp = await self.client.get(
                    f"{self.base_url}/NORAD/elements/gp.php",
                    params={"CATNR": norad, "FORMAT": "json"},
                )
                payload = resp.json()
            except httpx.HTTPStatusError as e:
                self._fail(f"HTTP {e.response.status_code}")
                continue
            except (httpx.HTTPError, CircuitOpenError, ValueError) as e:
                self._fail(f"{type(e).__name__}: {e}")
                continue
            events, dropped = parse_gp(payload)
            self.n_dropped += dropped
            if dropped:
                DROPPED.labels(self.name).inc(dropped)
            if not events:
                self._fail(f"no usable elements for {norad}")
                continue
            self.n_ok += 1
            self.last_ok = self._now()
            latest = max(events, key=lambda e: e.ts)  # one record expected; be safe
            for ev in events:
                out[ev.id] = ev
            omm = {**latest.attrs["omm"], "OBJECT_NAME": meta.get("name", latest.attrs["name"])}
            omms.append((omm, dict(meta)))
        self._omms = omms
        out.update(self._passes(start))
        out.update(await self._poll_groups())
        self.records_hour.add(len(out))
        RECORDS_IN.labels(self.name).inc(len(out))
        return list(out.values())

    async def stream(self) -> AsyncIterator[Sequence[BaseModel]]:
        self._wake = asyncio.Event()
        next_full = 0.0
        loop = asyncio.get_running_loop()
        while not self._stop:
            if loop.time() >= next_full:
                batch: list[GeoEvent] = await self.poll_once()
                next_full = loop.time() + self.poll_s
            else:  # woken by an area edit: passes from the cached elements only
                batch = list(self._passes(self._now()).values())
            self._dirty = False
            if batch:
                yield batch
            if self._sleep is not asyncio.sleep:  # tests inject a fake sleep
                await self._sleep(self.poll_s)
                next_full = 0.0
                continue
            self._wake.clear()
            if not self._dirty:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), max(1.0, next_full - loop.time()))

    async def aclose(self) -> None:
        self._stop = True
        if self._client is not None:
            await self._client.aclose()


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return CelestrakConnector(
        key,
        str(cfg["base"]),
        settings.aois,
        cfg.get("satellites", {}),
        poll_s=float(cfg.get("poll_s", 86400)),
        pass_horizon_h=float(cfg.get("pass_horizon_h", 48)),
        min_elev_deg=float(cfg.get("min_elev_deg", 20)),
        rps=float(cfg.get("rps", 0.5)),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 86400)),
        groups=cfg.get("groups") or {},
    )
