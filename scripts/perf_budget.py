"""API latency budget gate (task 063/064): concurrent GETs, p50/p95 per endpoint, fail over budget.

  uv run python scripts/perf_budget.py --base http://127.0.0.1:8000
  HEIMDALL_BENCH_CODE=... uv run python scripts/perf_budget.py --base https://dev.heimdall.example.org

Hosted instances need an access code (env HEIMDALL_BENCH_CODE, never a CLI argument so it stays out
of process lists and CI logs). Budgets: config/perf_budget.yaml (p95 ms per endpoint).
Exit code 1 when any endpoint's p95 is over budget or any request fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]


async def _hit(c: httpx.AsyncClient, path: str, n: int, conc: int) -> tuple[list[float], int]:
    sem = asyncio.Semaphore(conc)
    times: list[float] = []
    errors = 0

    async def one() -> None:
        nonlocal errors
        async with sem:
            t = time.perf_counter()
            try:
                r = await c.get(path)
                ok = r.status_code == 200
            except httpx.HTTPError:
                ok = False
            times.append((time.perf_counter() - t) * 1000)
            errors += 0 if ok else 1

    await asyncio.gather(*(one() for _ in range(n)))
    return times, errors


def _p(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


async def main_async(base: str, n: int, conc: int, budget: dict[str, float]) -> int:
    async with httpx.AsyncClient(base_url=base, timeout=30, follow_redirects=False) as c:
        if code := os.environ.get("HEIMDALL_BENCH_CODE"):
            r = await c.post("/api/login", json={"code": code}, headers={"Origin": base})
            if r.status_code != 200:
                print(f"login failed: {r.status_code}", file=sys.stderr)
                return 1
        failed = False
        print(f"{'endpoint':<48} {'p50':>7} {'p95':>7} {'budget':>7}  errors")
        for path, limit in budget.items():
            try:
                await c.get(path)  # warm caches
            except httpx.HTTPError as e:
                print(f"{path}: unreachable ({type(e).__name__})", file=sys.stderr)
                return 1
            times, errors = await _hit(c, path, n, conc)
            p50, p95 = statistics.median(times), _p(times, 0.95)
            bad = p95 > limit or errors > 0
            failed |= bad
            flag = "  OVER" if bad else ""
            print(f"{path:<48} {p50:7.0f} {p95:7.0f} {limit:7.0f}  {errors}{flag}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("-n", type=int, default=40, help="requests per endpoint")
    ap.add_argument("-c", type=int, default=8, help="concurrency")
    ap.add_argument("--budget", type=Path, default=ROOT / "config" / "perf_budget.yaml")
    a = ap.parse_args(argv)
    budget = {str(k): float(v) for k, v in yaml.safe_load(a.budget.read_text())["p95_ms"].items()}
    return asyncio.run(main_async(a.base.rstrip("/"), a.n, a.c, budget))


if __name__ == "__main__":
    raise SystemExit(main())
