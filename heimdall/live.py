"""Live position index (task 026a): latest fix per entity, spatial grid, incremental density.

Pure Python (no duckdb/httpx): the runtime writer feeds it on every flush, the API reads it.
Thread-safe (one lock per layer); entries are replaced, never mutated, so a reader holding a
reference always sees a consistent fix.

Expiry is keyed on the **insertion clock** (`now` given to `update_*` / `expire`), never on the
observation time, so replayed data with old timestamps is not dropped (same rule as Writer).
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from heimdall.geo import BBox, h3_cell, h3_parent
from heimdall.schemas import AircraftObs, VesselObs

AIR_MAX_AGE_S = 60.0
SEA_MAX_AGE_S = 900.0
GRID_DEG = 1.0
EXPIRE_EVERY_S = 5.0
TOMBSTONE_VERSIONS = 1000  # deltas older than this many flushes fall back to a full response


def is_military_ship(ship_type: int | None) -> bool:
    return ship_type == 35


def gps_bad(o: AircraftObs) -> bool:
    return (o.nic is not None and o.nic < 7) or (o.nac_p is not None and o.nac_p < 8)


class Entry:
    """Latest fix of one entity plus the flags the API filters/aggregates on."""

    __slots__ = (
        "frag",
        "gps_bad",
        "is_global",
        "key",
        "lat",
        "lon",
        "mil",
        "obs",
        "provider",
        "providers",
        "r3",
        "r4",
        "row",
        "seen",
        "ts",
        "ver",
    )

    def __init__(
        self,
        key: Any,
        obs: AircraftObs | VesselObs,
        mil: bool,
        bad: bool,
        providers: frozenset[str],
        seen: datetime,
        ver: int,
    ) -> None:
        self.key = key
        self.obs = obs
        self.lat, self.lon, self.ts = obs.lat, obs.lon, obs.ts
        self.mil = mil
        self.gps_bad = bad
        self.provider = obs.provider
        self.providers = providers
        self.is_global = obs.is_global
        self.seen = seen
        self.ver = ver
        self.row: tuple[Any, ...] | None = None  # API-owned cache (compact format)
        self.frag: str | None = None  # API-owned cache of the serialised static properties
        self.r4 = obs.h3_r4 or h3_cell(obs.lat, obs.lon, 4)
        self.r3 = h3_parent(self.r4, 3)


def _pkey(provider: str) -> str:
    return provider.lower().replace(".", "_")


def matches_sources(e: Entry, want: set[str] | None) -> bool:
    if want is None:
        return True
    return any(_pkey(p) in want for p in e.providers) or _pkey(e.provider) in want


class Layer:
    """One entity kind (air or sea)."""

    def __init__(self, max_age_s: float) -> None:
        self.max_age_s = max_age_s
        self.version = 0
        self._entries: dict[Any, Entry] = {}
        self._grid: dict[tuple[int, int], dict[Any, Entry]] = {}
        self._dens: dict[int, dict[str, list[int]]] = {3: {}, 4: {}}
        self._tomb: deque[tuple[int, Any]] = deque()
        self._floor = 0  # highest version whose tombstones were pruned
        self._last_expire: datetime | None = None
        self._lock = threading.Lock()
        self._dcache: dict[tuple[int, int], list[dict[str, Any]]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _cell(lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / GRID_DEG), math.floor(lon / GRID_DEG)

    def _remove(self, e: Entry) -> None:
        b = self._grid.get(self._cell(e.lat, e.lon))
        if b is not None:
            b.pop(e.key, None)
            if not b:
                del self._grid[self._cell(e.lat, e.lon)]
        for res, cell in ((3, e.r3), (4, e.r4)):
            d = self._dens[res]
            c = d.get(cell)
            if c is not None:
                c[0] -= 1
                c[1] -= int(e.mil)
                if c[0] <= 0:
                    del d[cell]

    def _add(self, e: Entry) -> None:
        self._grid.setdefault(self._cell(e.lat, e.lon), {})[e.key] = e
        for res, cell in ((3, e.r3), (4, e.r4)):
            c = self._dens[res].setdefault(cell, [0, 0])
            c[0] += 1
            c[1] += int(e.mil)

    def update(
        self,
        items: Iterable[tuple[Any, AircraftObs | VesselObs, bool, bool, frozenset[str]]],
        now: datetime,
    ) -> int:
        """Apply `(key, obs, mil, gps_bad, providers)` tuples; returns the number applied."""
        n = 0
        with self._lock:
            ver = self.version + 1
            for key, obs, mil, bad, provs in items:
                old = self._entries.get(key)
                if old is not None and obs.ts < old.ts:
                    continue  # out-of-order fix
                e = Entry(key, obs, mil, bad, provs, now, ver)
                if old is not None:
                    self._remove(old)
                self._entries[key] = e
                self._add(e)
                n += 1
            if n:
                self.version = ver
                self._dcache.clear()
            self._expire_locked(now)
        return n

    def expire(self, now: datetime) -> int:
        with self._lock:
            return self._expire_locked(now)

    def _expire_locked(self, now: datetime) -> int:
        if self._last_expire is not None and (now - self._last_expire).total_seconds() < (
            EXPIRE_EVERY_S
        ):
            return 0
        self._last_expire = now
        cutoff = now - timedelta(seconds=self.max_age_s)
        dead = [e for e in self._entries.values() if e.seen < cutoff]
        if not dead:
            return 0
        self.version += 1
        for e in dead:
            self._remove(e)
            del self._entries[e.key]
            self._tomb.append((self.version, e.key))
        while self._tomb and self._tomb[0][0] <= self.version - TOMBSTONE_VERSIONS:
            self._floor = self._tomb.popleft()[0]
        self._dcache.clear()
        return len(dead)

    # ------------------------------------------------------------------ reads
    def _candidates(self, bb: BBox) -> list[Entry]:
        w, s, e, n = bb
        ranges = [(w, e)] if w <= e else [(w, 180.0), (-180.0, e)]
        i0, i1 = self._cell(s, 0)[0], self._cell(n, 0)[0]
        out: list[Entry] = []
        for a, b in ranges:
            j0, j1 = self._cell(0, a)[1], self._cell(0, b)[1]
            if (i1 - i0 + 1) * (j1 - j0 + 1) > len(self._grid):  # wide: walk occupied buckets
                keys: Iterable[tuple[int, int]] = [
                    k for k in self._grid if i0 <= k[0] <= i1 and j0 <= k[1] <= j1
                ]
            else:
                keys = ((bi, bj) for bi in range(i0, i1 + 1) for bj in range(j0, j1 + 1))
            for bi, bj in keys:
                bucket = self._grid.get((bi, bj))
                if not bucket:
                    continue
                if (
                    s <= bi * GRID_DEG
                    and (bi + 1) * GRID_DEG <= n
                    and a <= bj * GRID_DEG
                    and (bj + 1) * GRID_DEG <= b
                ):  # bucket wholly inside the box: no per-entry test
                    out.extend(bucket.values())
                else:
                    out.extend(x for x in bucket.values() if s <= x.lat <= n and a <= x.lon <= b)
        return out

    def query(
        self,
        bb: BBox,
        now: datetime,
        max_age_s: float,
        want: set[str] | None = None,
        since: int | None = None,
        future: timedelta = timedelta(minutes=5),
        limit: int | None = None,
    ) -> list[Entry]:
        """Fresh entries in `bb`; `since` keeps only those changed after it.

        Sorted newest first only when more than `limit` match (so the cap keeps the newest);
        otherwise in arbitrary order.
        """
        lo, hi = now - timedelta(seconds=max_age_s), now + future
        with self._lock:
            cand = self._candidates(bb)
        out = [e for e in cand if lo <= e.ts <= hi]
        if since is not None:
            out = [e for e in out if e.ver > since]
        if want is not None:
            out = [e for e in out if matches_sources(e, want)]
        if limit is not None and len(out) > limit:
            out.sort(key=lambda e: e.ts, reverse=True)
        return out

    def removed_since(self, since: int) -> list[Any] | None:
        """Ids expired after version `since`, or None when the delta window was pruned."""
        with self._lock:
            if since < self._floor:
                return None
            return [k for v, k in self._tomb if v > since and k not in self._entries]

    def density_world(self, res: int) -> list[dict[str, Any]]:
        """Maintained per-cell counts for the whole index (cached until the next change)."""
        with self._lock:
            key = (self.version, res)
            hit = self._dcache.get(key)
            if hit is None:
                hit = [{"h3": k, "count": v[0], "mil": v[1]} for k, v in self._dens[res].items()]
                self._dcache = {key: hit}
            return hit

    def total(self) -> int:
        return len(self._entries)


class LivePositions:
    """Air + sea layers fed by the writer, read by `heimdall.api.traffic`."""

    def __init__(
        self, air_max_age_s: float = AIR_MAX_AGE_S, sea_max_age_s: float = SEA_MAX_AGE_S
    ) -> None:
        self.air = Layer(air_max_age_s)
        self.sea = Layer(sea_max_age_s)

    def update_air(
        self,
        rows: Iterable[AircraftObs],
        now: datetime,
        providers: Mapping[str, set[str]] | None = None,
    ) -> int:
        def items() -> Iterable[tuple[Any, AircraftObs, bool, bool, frozenset[str]]]:
            for o in rows:
                p = frozenset(providers[o.hex]) if providers and o.hex in providers else None
                yield o.hex, o, o.mil, gps_bad(o), p or frozenset({o.provider})

        return self.air.update(items(), now)

    def update_sea(self, rows: Iterable[VesselObs], now: datetime) -> int:
        def items() -> Iterable[tuple[Any, VesselObs, bool, bool, frozenset[str]]]:
            for v in rows:
                yield v.mmsi, v, is_military_ship(v.ship_type), False, frozenset({v.provider})

        return self.sea.update(items(), now)

    def expire(self, now: datetime) -> None:
        self.air.expire(now)
        self.sea.expire(now)

    @property
    def version(self) -> tuple[int, int]:
        return self.air.version, self.sea.version
