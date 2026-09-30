"""GNSS interference map service (task 031): rolling last-24 h + finished UTC days.

`LakeGnssService` (live runtime) computes from the Parquet lake via `adapters.gnss_lake` and writes
each finished UTC day once. `StoreGnssService` (demo / replay) computes from the Store's aircraft
window, so the UI has a map without a lake. Both return `heimdall.gnss_map.GnssDay`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from heimdall.adapters.gnss_lake import GnssLake
from heimdall.gnss_map import GnssDay, aggregate, counts_from_obs
from heimdall.logging import get_logger
from heimdall.ports import Store

log = get_logger("heimdall.gnss")

LAST24H = "last24h"
DEFAULTS: dict[str, Any] = {
    "dwell_frac": 0.5,  # same as detectors.jamming.bad_dwell_frac
    "exclude_military": True,  # GPSJam FAQ: trainers block their own antennas in hard manoeuvres
    "refresh_min": 15,
    "min_hours_complete": 22,  # < 22 of 24 hourly lake partitions -> "incomplete day"
    "backfill_days": 7,  # finished days written retroactively when lake data exists
    "store_window_h": 24,
}


class GnssService(Protocol):
    def day(self, key: str) -> GnssDay | None: ...

    def days(self) -> list[str]: ...


def _cfg(detectors: dict[str, Any]) -> dict[str, Any]:
    jam = detectors.get("jamming", {})
    c = {**DEFAULTS, **detectors.get("gnss_map", {})}
    c["nic_below"] = int(jam.get("nic_bad_below", 7))
    c["nacp_below"] = int(jam.get("nacp_bad_below", 8))
    return c


class LakeGnssService:
    def __init__(self, lake: GnssLake, detectors: dict[str, Any], sources: list[str]) -> None:
        self.lake, self.c, self.sources = lake, _cfg(detectors), sources
        self.last24h: GnssDay | None = None

    def compute(self, start: datetime, end: datetime, key: str) -> GnssDay:
        counts = self.lake.counts(start, end, self.c["nic_below"], self.c["nacp_below"])
        cells = aggregate(
            counts, dwell_frac=self.c["dwell_frac"], exclude_military=self.c["exclude_military"]
        )
        hours = self.lake.hours_with_data(start, end)
        return GnssDay(
            date=key,
            cells=cells,
            hours_with_data=hours,
            incomplete=hours < int(self.c["min_hours_complete"]),
            sources=self.sources,
        )

    def refresh(self, now: datetime) -> None:
        now = now.astimezone(UTC)
        self.last24h = self.compute(now - timedelta(hours=24), now, LAST24H)
        today = now.date()
        for k in range(1, int(self.c["backfill_days"]) + 1):
            d = today - timedelta(days=k)
            if self.lake.has_day(d.isoformat()):
                continue
            start = datetime(d.year, d.month, d.day, tzinfo=UTC)
            if self.lake.hours_with_data(start, start + timedelta(days=1)) == 0:
                continue
            self.lake.write_day(self.compute(start, start + timedelta(days=1), d.isoformat()))
            log.info("gnss_day_written", date=d.isoformat())

    def day(self, key: str) -> GnssDay | None:
        return self.last24h if key == LAST24H else self.lake.read_day(key)

    def days(self) -> list[str]:
        return self.lake.days()


class StoreGnssService:
    """Demo/replay: computed on request from the Store's aircraft window (cached 60 s)."""

    def __init__(self, store: Store, detectors: dict[str, Any]) -> None:
        self.store, self.c = store, _cfg(detectors)
        self._cache: tuple[datetime, GnssDay] | None = None

    def day(self, key: str) -> GnssDay | None:
        if key != LAST24H:
            return None
        now = datetime.now(UTC)
        if self._cache and now - self._cache[0] < timedelta(seconds=60):
            return self._cache[1]
        obs = self.store.aircraft_window(now - timedelta(hours=self.c["store_window_h"]), now)
        cells = aggregate(
            counts_from_obs(obs, self.c["nic_below"], self.c["nacp_below"]),
            dwell_frac=self.c["dwell_frac"],
            exclude_military=self.c["exclude_military"],
        )
        d = GnssDay(date=LAST24H, cells=cells, sources=sorted({o.provider for o in obs}))
        self._cache = (now, d)
        return d

    def days(self) -> list[str]:
        return []


async def gnss_loop(svc: LakeGnssService, refresh_min: float | None = None) -> None:
    """Runtime task: recompute last-24 h (and write finished days) every `refresh_min`."""
    period = float(refresh_min or svc.c["refresh_min"]) * 60
    while True:
        try:
            await asyncio.to_thread(svc.refresh, datetime.now(UTC))
        except Exception as e:  # a bad partition must not stop the runtime
            log.warning("gnss_refresh_failed", error=f"{type(e).__name__}: {e}")
        await asyncio.sleep(period)


def valid_key(key: str) -> bool:
    if key == LAST24H:
        return True
    try:
        date.fromisoformat(key)
    except ValueError:
        return False
    return len(key) == 10
