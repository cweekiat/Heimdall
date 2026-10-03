"""Replay the HF path detector over backfilled WSPR bins (P6 calibration, task 045).

Steps the newest-window end every `--step-min` over [--start, --end) and reports, per run, the
cells that would reach fusion (flagged, unexplained, persistence >= --persist) and what the
natural explanations removed. Flares in the past are not in the 6-hour SWPC feed, so known
flares are given with --flare "2025-05-14T07:47/08:10/08:25=2.7e-4" (start/peak/end=peak flux,
from NOAA SWPC event reports); Kp comes from the lake when present.

Usage: uv run python scripts/hf_replay.py data/replay/hf/wspr_2026-09.jsonl \
         --start 2026-09-27 --end 2026-10-03 [--flare ...] [--set key=value ...]
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from heimdall.detect import hf
from heimdall.ingest.swpc import flare_class
from heimdall.schemas import Domain, GeoEvent


def load(path: Path) -> list[hf.SpotBin]:
    with path.open() as f:
        return hf.bins_from_events(GeoEvent.model_validate_json(line) for line in f if line.strip())


def flare_events(spec: str) -> list[GeoEvent]:
    """'START/PEAK/END=FLUX' -> 5-min solar_xray events, flux rising to the peak and decaying."""
    times, flux = spec.split("=")
    t0, tp, t1 = (datetime.fromisoformat(x).replace(tzinfo=UTC) for x in times.split("/"))
    peak = float(flux)
    out, t = [], t0
    while t <= t1:
        frac = (t - t0) / (tp - t0) if t <= tp else 1 - (t - tp) / (t1 - tp + timedelta(minutes=5))
        f = max(1e-6, peak * max(frac, 0.05))
        out.append(
            GeoEvent(
                id=f"flare|{t:%Y%m%dT%H%M}",
                ts=t,
                source="swpc",
                domain=Domain.SPACE,
                kind="solar_xray",
                value=f,
                attrs={"flux_wm2": f, "flare_class": flare_class(f)},
            )
        )
        t += timedelta(minutes=5)
    return out


def kp_from_lake(lo: datetime, hi: datetime) -> list[GeoEvent]:
    try:
        import duckdb

        rows = duckdb.sql(
            "SELECT ts, value FROM read_parquet('data/lake/events/**/*.parquet', "
            "union_by_name=true, hive_partitioning=true) "
            f"WHERE kind = 'space_weather' AND ts >= '{lo.isoformat()}' AND ts < '{hi.isoformat()}'"
        ).fetchall()
    except Exception:
        return []
    return [
        GeoEvent(
            id=f"kp|{ts}",
            ts=ts,
            source="swpc",
            domain=Domain.SPACE,
            kind="space_weather",
            attrs={"kp": float(v)},
        )
        for ts, v in rows
        if v is not None
    ]


def main(a: argparse.Namespace) -> None:
    cfg: dict[str, object] = {}
    for kv in a.set or []:
        k, v = kv.split("=")
        cfg[k] = float(v)
    bins = load(Path(a.path))
    start = datetime.fromisoformat(a.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(a.end).replace(tzinfo=UTC)
    ctx = kp_from_lake(start - timedelta(days=1), end)
    for spec in a.flare or []:
        ctx += flare_events(spec)
    by_t: dict[datetime, list[hf.SpotBin]] = defaultdict(list)
    for b in bins:
        by_t[b.t].append(b)
    runs = alerts = 0
    raised: Counter[tuple[str, int, float, float]] = Counter()
    explained: Counter[str] = Counter()
    hours: Counter[int] = Counter()
    t = start
    while t < end:
        st = hf.prepare(by_t_iter(by_t, t, cfg), ctx, t, cfg)
        runs += 1
        for key, f in st.flags[0].items():
            for x in f.explained:
                explained[x.split(" (")[0].split(" at ")[0]] += 1
            if not f.flagged:
                continue
            pers = 0
            for w in st.flags:
                if key in w and w[key].flagged:
                    pers += 1
                else:
                    break
            if pers >= a.persist:
                alerts += 1
                raised[key] += 1
                hours[t.hour] += 1
                if a.verbose:
                    print(
                        f"{t:%m-%d %H:%M} {key} n={f.n:.0f}/{f.usual_n:.0f} "
                        f"snr={f.snr} vs {f.usual_snr} rx={f.usual_rx:.0f}"
                    )
        t += timedelta(minutes=a.step_min)
    print(f"runs {runs}, alerting cell-runs {alerts}, distinct cells {len(raised)}")
    print("by UTC hour:", dict(sorted(hours.items())))
    print("top cells:", raised.most_common(8))
    print("explained:", dict(explained))


def by_t_iter(by_t: dict[datetime, list[hf.SpotBin]], t_end: datetime, cfg: dict[str, object]):  # type: ignore[no-untyped-def]
    sp = hf.span(cfg)
    days = int(float(cfg.get("baseline_days", hf.DEFAULTS["baseline_days"])))  # type: ignore[arg-type]
    for d in range(days + 1):
        lo = t_end - timedelta(days=d) - sp
        t = lo
        while t < lo + sp:
            yield from by_t.get(t, [])
            t += timedelta(minutes=10)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--step-min", type=int, default=30)
    p.add_argument("--persist", type=int, default=3)
    p.add_argument("--flare", action="append")
    p.add_argument("--set", action="append")
    p.add_argument("-v", "--verbose", action="store_true")
    main(p.parse_args())
