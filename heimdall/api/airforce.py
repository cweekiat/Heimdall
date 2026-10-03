"""Air force watch API (task 056). Read-only.

GET /api/airforces            -> [{key, name, short}]
GET /api/airforces/{key}?days -> fleet picture (heimdall.airforce.analyse): aircraft and where they
    are, movements,
                                 flights per day, notes, limits
Positions: the Parquet lake (worldwide military rows `global/aircraft` and area rows `aircraft`, up
    to 7 days),
one point per aircraft per minute, plus the last 6 h of the hot store. Finished hour partitions are
    read once and
kept per fleet (they never change); only the current hour is re-read.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import yaml
from fastapi import APIRouter, HTTPException, Query

from heimdall import airforce, config
from heimdall.adapters.parquet_lake import ParquetLake
from heimdall.ingest import ourairports, plane_alert
from heimdall.milactivity import Point
from heimdall.milroles import classify
from heimdall.ports import Store

PATH = Path(__file__).resolve().parents[2] / "config" / "air_forces.yaml"
DATASETS = ("global/aircraft", "aircraft")
CACHE_S = 300.0


@lru_cache(maxsize=1)
def forces() -> dict[str, dict[str, Any]]:
    try:
        raw = yaml.safe_load(PATH.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return {str(k): dict(v) for k, v in (raw.get("air_forces") or {}).items()}


def fleet_of(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    pats = [str(p).lower() for p in cfg.get("operators") or []]
    out: dict[str, dict[str, Any]] = {}
    for h, e in plane_alert.index().items():
        op = (e.get("operator") or "").lower()
        if any(p in op for p in pats):
            i = classify(h, e.get("icao_type") or None, e)
            out[h] = {
                "registration": i.registration,
                "type": (e.get("icao_type") or "").upper() or None,
                "type_name": i.type_name,
                "role": i.role,
                "operator": i.operator,
            }
    return out


class LakeFleetReader:
    """Per-fleet cache of one-point-per-minute positions by lake hour partition."""

    def __init__(self, lake_dir: str | Path) -> None:
        self.lake = ParquetLake(lake_dir)
        self.parts: dict[tuple[str, str], list[tuple[str, Point, str | None]]] = {}
        self.lock = threading.Lock()

    def _read(self, files: list[str], hexes: list[str]) -> list[tuple[str, Point, str | None]]:
        if not files:
            return []
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT hex, epoch(ts), lat, lon, alt_baro, gs, on_ground, flight "
                "FROM read_parquet(?, union_by_name=true) WHERE hex IN (SELECT unnest(?)) "
                "QUALIFY row_number() OVER (PARTITION BY hex, "
                "time_bucket(INTERVAL '60 seconds', ts) ORDER BY ts) = 1",
                [files, hexes],
            ).fetchall()
        finally:
            con.close()
        out = []
        for h, t, lat, lon, alt, gs, ground, flight in rows:
            a = -1.0 if ground or alt is None else float(alt)
            out.append(
                (
                    h,
                    (float(t), float(lat), float(lon), a, float(gs) if gs is not None else -1.0),
                    flight,
                )
            )
        return out

    def history(self, hex_: str, start: datetime, end: datetime) -> list[Point]:
        """One aircraft's positions from the lake (not cached): investigation fallback."""
        files = [f for ds in DATASETS for f in self.lake.paths(ds, start, end)]
        return sorted(
            p
            for h, p, _ in self._read(files, [hex_])
            if start.timestamp() <= p[0] <= end.timestamp()
        )

    def positions(
        self, key: str, hexes: list[str], start: datetime, end: datetime
    ) -> list[tuple[str, Point, str | None]]:
        out: list[tuple[str, Point, str | None]] = []
        cur_hour = end.astimezone(UTC).strftime("%Y-%m-%d %H")
        t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        keep: set[tuple[str, str]] = set()
        with self.lock:
            while t <= end:
                hour = t.strftime("%Y-%m-%d %H")
                ck = (key, hour)
                keep.add(ck)
                if ck in self.parts and hour != cur_hour:
                    out += self.parts[ck]
                else:
                    files = [f for ds in DATASETS for f in self.lake.paths(ds, t, t)]
                    rows = self._read(files, hexes)
                    if hour != cur_hour:
                        self.parts[ck] = rows
                    out += rows
                t += timedelta(hours=1)
            for k in [k for k in self.parts if k[0] == key and k not in keep]:
                del self.parts[k]  # slid out of the window
        return out


def picture(
    store: Store, reader: LakeFleetReader, key: str, days: float, now: datetime
) -> dict[str, Any]:
    cfg = forces().get(key)
    if cfg is None:
        raise HTTPException(404, "unknown air force")
    fleet = fleet_of(cfg)
    hexes = sorted(fleet)
    tracks: dict[str, dict[float, Point]] = {h: {} for h in hexes}
    callsign: dict[str, tuple[float, str]] = {}
    start = now - timedelta(days=days)
    for h, p, flight in reader.positions(key, hexes, start, now):
        tracks[h][p[0]] = p
        if flight and flight.strip() and p[0] >= callsign.get(h, (0.0, ""))[0]:
            callsign[h] = (p[0], flight.strip())
    for o in store.aircraft_window(now - timedelta(hours=6), now, ident=frozenset(hexes)):
        t = o.ts.timestamp()
        alt = -1.0 if o.on_ground or o.alt_baro is None else float(o.alt_baro)
        tracks[o.hex][t] = (t, o.lat, o.lon, alt, float(o.gs) if o.gs is not None else -1.0)
        if o.flight and o.flight.strip() and t >= callsign.get(o.hex, (0.0, ""))[0]:
            callsign[o.hex] = (t, o.flight.strip())
    for h, (_, fl) in callsign.items():
        fleet[h] = {**fleet[h], "flight": fl}
    pts = {h: sorted(v.values()) for h, v in tracks.items() if v}
    out = airforce.analyse(fleet, pts, ourairports.index().nearest, cfg, now)
    out.update(
        key=key,
        name=cfg.get("name"),
        short=cfg.get("short"),
        days=days,
        home_bases=cfg.get("home_bases"),
        detachments=cfg.get("detachments"),
        not_seen=[
            {"hex": h, **{k: fleet[h].get(k) for k in ("registration", "type", "type_name")}}
            for h in hexes
            if h not in pts
        ],
        attribution="Fleet list: plane-alert-db (ODbL); airfields: OurAirports (public domain)",
    )
    return out


def airforce_router(store: Store) -> APIRouter:
    r = APIRouter()
    reader = LakeFleetReader(config.settings().storage.lake_dir)
    cache: dict[tuple[str, float], tuple[float, dict[str, Any]]] = {}

    @r.get("/api/airforces")
    def list_forces() -> list[dict[str, Any]]:
        return [
            {"key": k, "name": v.get("name"), "short": v.get("short")} for k, v in forces().items()
        ]

    @r.get("/api/airforces/{key}")
    def get_force(key: str, days: float = Query(3.0, ge=0.5, le=7.0)) -> dict[str, Any]:
        hit = cache.get((key, days))
        if hit is not None and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        out = picture(store, reader, key, days, datetime.now(UTC))
        cache[(key, days)] = (time.monotonic(), out)
        return out

    return r
