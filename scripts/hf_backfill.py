"""Backfill WSPR path-midpoint spot bins for HF baselines (P6, task 045).

The HF detector compares each path-midpoint cell and band with the same clock time on the previous
14 days, so a fresh install has nothing to compare against. WSPR Live keeps the full archive, so
this runs the connector's own server-side aggregation (no raw spots, no callsigns) over past days.

Output: a JSON-lines file of `hf_spots` GeoEvents (replays, backtests) and, with --store, the same
events inserted into the hot store (stop `heimdall.run` first: DuckDB locks the file).

Usage: uv run python scripts/hf_backfill.py --start 2026-09-15 --end 2026-10-02
       [--out data/replay/hf/wspr.jsonl] [--store data/heimdall.duckdb] [--chunk-h 6]
       uv run python scripts/hf_backfill.py --from-file data/replay/hf/wspr.jsonl \
         --store data/heimdall.duckdb
       (insert an earlier download without fetching again)
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx

from heimdall import config
from heimdall.ingest.wspr import DEFAULT_BBOX, build_query, parse_wspr
from heimdall.schemas import GeoEvent


def insert(events: list[GeoEvent], store_path: str) -> None:
    from heimdall.adapters.duckdb_store import DuckDBStore

    st = config.settings().storage
    store = DuckDBStore(store_path, memory_limit=st.duckdb_memory_limit, threads=st.duckdb_threads)
    n = 0
    for i in range(0, len(events), 20_000):  # chunks keep memory flat
        n += store.insert_events(events[i : i + 20_000])
    print(f"inserted {n} events into {store_path}")


async def main(a: argparse.Namespace) -> None:
    if a.from_file:
        with open(a.from_file) as f:
            evs = [GeoEvent.model_validate_json(line) for line in f if line.strip()]
        if not a.store:
            raise SystemExit("--from-file needs --store")
        insert(evs, a.store)
        return
    src = config.sources()["sources"]["wspr_live"]
    base = str(src["base"]).rstrip("/")
    bbox = tuple(float(v) for v in src.get("bbox", DEFAULT_BBOX))
    grid, bin_min = float(src.get("grid_deg", 2.0)), int(src.get("bin_min", 10))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    t = datetime.combine(a.start, time(0), UTC)
    end = datetime.combine(a.end + timedelta(days=1), time(0), UTC)
    events = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        while t < end:
            t1 = min(t + timedelta(hours=a.chunk_h), end)
            q = build_query(
                t,
                t1,
                bbox=bbox,  # type: ignore[arg-type]
                grid_deg=grid,
                bin_min=bin_min,
                min_km=int(src.get("min_km", 500)),
                max_km=int(src.get("max_km", 12000)),
            )
            r = await client.get(f"{base}/", params={"query": q})
            r.raise_for_status()
            evs, dropped = parse_wspr(r.json(), bin_min=bin_min, grid_deg=grid, bbox=bbox)  # type: ignore[arg-type]
            events += evs
            print(f"{t:%Y-%m-%d %H:%M}: {len(evs)} bins ({dropped} dropped)", flush=True)
            t = t1
            await asyncio.sleep(1.0 / float(src.get("rps", 0.2)))
    with out.open("w") as f:
        for e in events:
            f.write(e.model_dump_json() + "\n")
    print(f"wrote {len(events)} events to {out}")
    if a.store:
        insert(events, a.store)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--from-file", help="insert this earlier download instead of fetching")
    p.add_argument("--out", default="data/replay/hf/wspr.jsonl")
    p.add_argument("--store", help="also insert into this DuckDB store (live server stopped)")
    p.add_argument("--chunk-h", type=int, default=6)
    asyncio.run(main(p.parse_args()))
