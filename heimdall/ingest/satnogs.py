"""SatNOGS Network observations (REST, no key) -> `sat_observation` GeoEvents (, space RF).

Each finished observation is a satellite pass recorded by a volunteer ground station, with a
status (good / bad / failed, or unknown until vetted). A run of `bad`/`failed` passes at stations
in one area, for satellites that other stations receive well, is the downlink-interference signal
a later detector looks for. Observations are placed at the ground station.

Polling: the station list (~3.5 MB) is refreshed once a day and reduced to stations inside
`bbox`; observations are paged from the global list by time window (cursor `Link: rel=next`,
capped at `max_pages`) and kept only for those stations. Re-polling a window upserts, so a later
vetting (unknown -> good/bad) replaces the earlier status. Free text (station name, observer) is
not stored; only ids, numbers and short enum strings.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from heimdall.config import Settings
from heimdall.ingest.poll import PollConnector, Unit, num, parse_dt
from heimdall.ingest.wspr import DEFAULT_BBOX, _bbox
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

STATUS_VALUE = {"good": 1.0, "unknown": 0.5, "bad": 0.0, "failed": -1.0}
_NEXT = re.compile(r'<(https://[^>]+)>;\s*rel="next"')
_TOKEN = re.compile(r"[^A-Za-z0-9 ._()/-]")


def _short(v: Any, n: int = 40) -> str | None:
    return _TOKEN.sub("", v)[:n] or None if isinstance(v, str) else None


def rf_band(freq_hz: float | None) -> str | None:
    if freq_hz is None:
        return None
    for name, lo, hi in (
        ("HF", 3e6, 30e6),
        ("VHF", 30e6, 300e6),
        ("UHF", 300e6, 1e9),
        ("L", 1e9, 2e9),
        ("S", 2e9, 4e9),
        ("C", 4e9, 8e9),
        ("X", 8e9, 12e9),
    ):
        if lo <= freq_hz < hi:
            return name
    return None


def parse_stations(
    payload: Any, bbox: tuple[float, float, float, float], now: datetime, active_days: float = 7
) -> dict[int, tuple[float, float]]:
    """Station id -> (lat, lon) for stations inside bbox seen within `active_days`."""
    out: dict[int, tuple[float, float]] = {}
    if not isinstance(payload, list):
        return out
    w, s, e, n = bbox
    cut = now - timedelta(days=active_days)
    for st in payload:
        if not isinstance(st, Mapping) or not isinstance(st.get("id"), int):
            continue
        lat, lon, seen = num(st.get("lat")), num(st.get("lng")), parse_dt(st.get("last_seen"))
        if lat is None or lon is None or seen is None or seen < cut:
            continue
        if s <= lat <= n and w <= lon <= e:
            out[st["id"]] = (lat, lon)
    return out


def parse_observations(
    payload: Any, stations: Mapping[int, tuple[float, float]]
) -> tuple[list[GeoEvent], int]:
    """Observation list -> (events for in-region, finished observations, n_dropped)."""
    if not isinstance(payload, list):
        return [], 0
    out: list[GeoEvent] = []
    dropped = 0
    for o in payload:
        if not isinstance(o, Mapping) or not isinstance(o.get("id"), int):
            dropped += 1
            continue
        gs = o.get("ground_station")
        status = o.get("status")
        if not isinstance(gs, int) or gs not in stations or status not in STATUS_VALUE:
            continue  # other region, or a future pass: not a drop
        start, end = parse_dt(o.get("start")), parse_dt(o.get("end"))
        if start is None or end is None:
            dropped += 1
            continue
        lat, lon = stations[gs]
        freq = num(o.get("observation_frequency")) or num(o.get("transmitter_downlink_low"))
        tle0 = o.get("tle0")
        out.append(
            GeoEvent(
                id=f"satnogs|obs|{o['id']}",
                ts=end,
                source="satnogs",
                domain=Domain.SPECTRUM,
                kind="sat_observation",
                lat=lat,
                lon=lon,
                valid_from=start,
                valid_to=end,
                title=f"SatNOGS pass {status}",
                value=STATUS_VALUE[status],
                attrs={
                    "role": "signal",
                    "status": status,
                    "waterfall_status": _short(o.get("waterfall_status"), 12),
                    "norad_cat_id": o.get("norad_cat_id")
                    if isinstance(o.get("norad_cat_id"), int)
                    else None,
                    "sat_name": _short(tle0[2:] if isinstance(tle0, str) else None),
                    "frequency_hz": freq,
                    "rf_band": rf_band(freq),
                    "mode": _short(o.get("transmitter_mode"), 30),
                    "max_altitude_deg": num(o.get("max_altitude")),
                    "rise_azimuth_deg": num(o.get("rise_azimuth")),
                    "set_azimuth_deg": num(o.get("set_azimuth")),
                    "station_id": gs,
                    "observation_id": o["id"],
                },
            )
        )
    return out, dropped


def next_link(link_header: str | None) -> str | None:
    m = _NEXT.search(link_header or "")
    return m.group(1) if m else None


class SatnogsConnector(PollConnector):
    domain = Domain.SPECTRUM

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
        lookback_h: float = 3,
        max_pages: int = 40,
        station_refresh_h: float = 24,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.base_url = base_url.rstrip("/")
        self.bbox, self.lookback_h, self.max_pages = bbox, lookback_h, max_pages
        self.station_refresh = timedelta(hours=station_refresh_h)
        self.stations: dict[int, tuple[float, float]] = {}
        self.stations_at: datetime | None = None

    async def _refresh_stations(self) -> None:
        now = self._now()
        if self.stations_at is not None and now - self.stations_at < self.station_refresh:
            return
        r = await self.client.get(f"{self.base_url}/api/stations/", params={"format": "json"})
        self.stations = parse_stations(r.json(), self.bbox, now)
        self.stations_at = now

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            await self._refresh_stations()
            now = self._now().astimezone(UTC)
            params: dict[str, str] | None = {
                "format": "json",
                "start": (now - timedelta(hours=self.lookback_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            url: str | None = f"{self.base_url}/api/observations/"
            events: list[GeoEvent] = []
            dropped = pages = 0
            while url and pages < self.max_pages:
                r = await self.client.get(url, params=params)
                ev, d = parse_observations(r.json(), self.stations)
                events += ev
                dropped += d
                pages += 1
                nxt = next_link(r.headers.get("link"))
                url = nxt if nxt and nxt.startswith(self.base_url) else None
                params = None  # the cursor link carries the query
            return events, dropped

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return SatnogsConnector(
        key,
        str(cfg["base"]),
        bbox=_bbox(cfg.get("bbox")),
        lookback_h=float(cfg.get("lookback_h", 3)),
        max_pages=int(cfg.get("max_pages", 40)),
        poll_s=float(cfg.get("poll_s", 1800)),
        rps=float(cfg.get("rps", 0.3)),
        reliability=SourceReliability(cfg.get("reliability", "C")),
        expected_latency_s=int(cfg.get("latency_s", 3600)),
    )
