"""026a benchmark: p50/p95 per traffic endpoint on synthetic 50k vessels + 10k aircraft.

Usage: uv run python scripts/bench_api.py [--reps 20]
Works before and after 026a: uses the live index when the router accepts one.
"""

from __future__ import annotations

import argparse
import inspect
import random
import statistics
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.api.traffic import traffic_router
from heimdall.geo import h3_cell
from heimdall.schemas import AircraftObs, VesselObs

BUSY = (90.0, -10.0, 130.0, 30.0)
NOW = datetime.now(UTC)


def synth(n_sea: int, n_air: int) -> tuple[list[VesselObs], list[AircraftObs]]:
    rnd = random.Random(7)

    def pos() -> tuple[float, float]:
        if rnd.random() < 0.6:
            return rnd.uniform(BUSY[1], BUSY[3]), rnd.uniform(BUSY[0], BUSY[2])
        return rnd.uniform(-60, 70), rnd.uniform(-179, 179)

    sea: list[VesselObs] = []
    air: list[AircraftObs] = []
    for i in range(n_sea):
        lat, lon = pos()
        sea.append(
            VesselObs(
                ts=NOW - timedelta(seconds=rnd.uniform(0, 800)),
                mmsi=200_000_000 + i,
                provider="aisstream",
                lat=lat,
                lon=lon,
                ship_type=rnd.choice([30, 35, 70, 80, 60]),
                sog=rnd.uniform(0, 20),
                h3_r4=h3_cell(lat, lon, 4),
                h3_r6=h3_cell(lat, lon, 6),
            )
        )
    for i in range(n_air):
        lat, lon = pos()
        air.append(
            AircraftObs(
                ts=NOW - timedelta(seconds=rnd.uniform(0, 55)),
                hex=f"{0x100000 + i:06x}",
                provider="adsb.lol",
                lat=lat,
                lon=lon,
                src_type="adsb_icao",
                mil=rnd.random() < 0.05,
                h3_r4=h3_cell(lat, lon, 4),
                h3_r6=h3_cell(lat, lon, 6),
            )
        )
    return sea, air


def pct(xs: list[float], q: float) -> float:
    return sorted(xs)[min(len(xs) - 1, int(q * len(xs)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--sea", type=int, default=50_000)
    ap.add_argument("--air", type=int, default=10_000)
    a = ap.parse_args()
    sea, air = synth(a.sea, a.air)
    store = DuckDBStore(":memory:")
    t0 = time.perf_counter()
    store.insert_vessels(sea)
    store.insert_aircraft(air)
    ins = time.perf_counter() - t0
    print(f"insert {a.sea + a.air} rows: {ins * 1000:.0f} ms")
    kw: dict[str, Any] = {}
    if "index" in inspect.signature(traffic_router).parameters:
        from heimdall.live import LivePositions

        idx = LivePositions()
        t0 = time.perf_counter()
        idx.update_air(air, NOW)
        idx.update_sea(sea, NOW)
        print(f"index update {a.sea + a.air} rows: {(time.perf_counter() - t0) * 1000:.0f} ms")
        kw = {"index": idx}
    app = FastAPI()
    app.include_router(traffic_router(store, now=lambda: NOW, **kw))
    c = TestClient(app)
    world = "-180,-85,180,85"
    busy = ",".join(str(v) for v in BUSY)
    cases = {
        "sea world density z3": f"/api/traffic/sea?bbox={world}&zoom=3",
        "air world density z3": f"/api/traffic/air?bbox={world}&zoom=3",
        "sea busy bbox z8 (20k)": f"/api/traffic/sea?bbox={busy}&zoom=8",
        "air busy bbox z8": f"/api/traffic/air?bbox={busy}&zoom=8",
    }
    print(f"{'endpoint':28s} {'p50 ms':>9s} {'p95 ms':>9s} {'bytes':>9s}")
    for name, url in cases.items():
        ts: list[float] = []
        size = 0
        for _ in range(a.reps):
            t = time.perf_counter()
            r = c.get(url)
            ts.append((time.perf_counter() - t) * 1000)
            size = len(r.content)
            assert r.status_code == 200, r.text
        print(f"{name:28s} {statistics.median(ts):9.1f} {pct(ts, 0.95):9.1f} {size:9d}")


if __name__ == "__main__":
    main()
