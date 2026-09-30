"""Run each configured connector the way the live app does and report what happens.

    uv run python scripts/diag_sources.py [seconds] [source ...]

Per source, it waits up to N seconds (default 45) for the first batch, then prints the row count
or the exception with its last traceback frame. Key values are never printed. Network goes only
to the hosts in config/sources.yaml.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import traceback

from heimdall import config
from heimdall.ingest.registry import build_connectors


async def probe(conn: object, seconds: float) -> str:
    async def first() -> int:
        async for batch in conn.stream():  # type: ignore[attr-defined]
            return len(batch)
        return -1

    try:
        n = await asyncio.wait_for(first(), seconds)
        res = "stream ended with no batch" if n < 0 else f"OK first batch {n} rows"
    except TimeoutError:
        res = f"no batch within {seconds:.0f}s"
    except Exception as e:  # report, don't raise
        tb = traceback.extract_tb(e.__traceback__)[-1]
        where = f"{tb.filename.split('heimdall/')[-1]}:{tb.lineno}"
        res = f"CRASH {type(e).__name__}: {str(e)[:300]} @ {where}"
    try:
        h = conn.health()  # type: ignore[attr-defined]
        res += f" | health={h.status} detail={h.detail!r} last_error={str(h.last_error)[:200]!r}"
    except Exception as e:
        res += f" | health() failed: {e}"
    with contextlib.suppress(Exception):
        await conn.aclose()  # type: ignore[attr-defined]
    return res


async def main(seconds: float, only: set[str]) -> None:
    settings = config.settings()
    src = config.sources()
    conns = build_connectors(settings, src, enabled=only or None)
    for c in conns:
        print(f"{c.name:22s} ...", flush=True)
        print(f"{c.name:22s} {await probe(c, seconds)}", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    secs = float(args.pop(0)) if args and args[0].replace(".", "").isdigit() else 45.0
    asyncio.run(main(secs, set(args)))
