"""Fill the CYGNSS reduction cache for past days (baselines and validation, task 038).

Fetches only the OPeNDAP subset (~8 MB per satellite-day), reduces it to per-cell values in
data/cache/cygnss/reduced/ and deletes the subset. Needs EARTHDATA_TOKEN (key store or env).
Already-cached granules are skipped, so it can be re-run.

Usage: uv run python scripts/cygnss_backfill.py --start 2026-09-10 --end 2026-09-29
       [--bbox W S E N] [--cache data/cache/cygnss]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from heimdall.config import secret
from heimdall.ingest.cygnss import DEFAULT_BBOX, CygnssConnector


async def main(a: argparse.Namespace) -> None:
    token = secret("EARTHDATA_TOKEN")
    if not token:
        raise SystemExit("EARTHDATA_TOKEN is not set (Data sources -> NASA Earthdata key)")
    c = CygnssConnector(
        "cygnss",
        "https://cmr.earthdata.nasa.gov",
        token=token,
        bbox=tuple(a.bbox) if a.bbox else DEFAULT_BBOX,
        cache_dir=Path(a.cache),
        rps=1.0,
        baseline_days=10_000,  # never prune here
    )
    try:
        d = a.start
        while d <= a.end:
            t0 = datetime.combine(d, time(0), UTC)
            found = await c.search(t0, t0 + timedelta(hours=23, minutes=59))
            todo = [g for g in found if not c.cached(g[0]).exists()]
            ok = 0
            for title, _, href in todo:
                try:
                    ok += (await c.fetch(title, href)) is not None
                except Exception as e:  # one bad granule must not stop the run
                    print(f"  {title[:5]} failed: {type(e).__name__}")
            print(f"{d}: {len(found)} granules, {len(todo)} new, {ok} reduced")
            d += timedelta(days=1)
    finally:
        await c.aclose()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument("--bbox", type=float, nargs=4)
    p.add_argument("--cache", default="data/cache/cygnss")
    asyncio.run(main(p.parse_args()))
