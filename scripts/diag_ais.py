"""Check the AISStream connection with the saved key (never printed). Run from the repo root:

    uv run python scripts/diag_ais.py [seconds]

Connects exactly as the live app does (same AOIs, same subscription), listens for N seconds
(default 60) and reports: key found or not, connection result, server errors (redacted),
messages received and vessels per AOI. Network: stream.aisstream.io only.
"""

from __future__ import annotations

import asyncio
import collections
import sys

from heimdall import config
from heimdall.geo import haversine_km
from heimdall.ingest.ais import build


async def main(seconds: float) -> None:
    settings = config.settings()
    cfg = config.sources()["sources"]["aisstream"]
    conn = build("aisstream", cfg, settings)
    key = config.secret(str(cfg.get("auth", "AISSTREAM_KEY")))
    print(f"key: {'found (' + str(len(key)) + ' chars)' if key else 'MISSING'}")
    if not key:
        return
    print(f"AOIs: {[a.name for a in settings.aois]}")
    print(f"subscription boxes: {len(conn.subscription()['BoundingBoxes'])}")  # type: ignore[attr-defined]
    per_aoi: collections.Counter[str] = collections.Counter()
    mmsis: set[int] = set()

    async def run() -> None:
        async for batch in conn.stream():
            for v in batch:
                mmsi = getattr(v, "mmsi", None)
                if mmsi is None:
                    continue
                mmsis.add(mmsi)
                for a in settings.aois:
                    if haversine_km(a.lat, a.lon, v.lat, v.lon) <= a.radius_nm * 1.852:
                        per_aoi[a.name] += 1

    task = asyncio.create_task(run())
    await asyncio.sleep(seconds)
    await conn.aclose()
    task.cancel()
    h = conn.health()
    print(f"connected at end: {getattr(conn, 'connected', '?')}")
    print(f"health: {h.status}  last_ok: {h.last_ok}  last_error: {h.last_error}")
    print(f"distinct vessels: {len(mmsis)}  position reports per AOI: {dict(per_aoi)}")


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 60.0))
