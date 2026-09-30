"""Local file I/O for real-data replays: observation parquet in, alert/evidence bundle out.

Offline only. `write_obs_parquet` / `read_obs_parquet` round-trip `AircraftObs` losslessly.
`write_bundle` persists a `ReplayResult` next to the input as `<stem>.alerts.jsonl` and
`<stem>.evidence.jsonl` (what `heimdall.api.demo --replay` loads) and appends the same rows to a
replay-local Parquet lake (`<dir>/lake`), never to the live `data/lake`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from heimdall.adapters.parquet_lake import ParquetLake
from heimdall.backtest.replay import ReplayResult
from heimdall.schemas import AircraftObs, Alert, Evidence


def write_obs_parquet(path: str | Path, obs: Iterable[AircraftObs]) -> int:
    """Write observations (sorted by time, hex) to one zstd parquet file; returns the row count."""
    rows = sorted(obs, key=lambda o: (o.ts, o.hex, o.provider))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([o.model_dump() for o in rows]), p, compression="zstd")
    return len(rows)


def read_obs_parquet(
    path: str | Path, start: datetime | None = None, end: datetime | None = None
) -> list[AircraftObs]:
    """Read observations back, optionally keeping `start <= ts < end`."""
    out = [AircraftObs.model_validate(r) for r in pq.read_table(path).to_pylist()]
    return [o for o in out if (start is None or o.ts >= start) and (end is None or o.ts < end)]


def bundle_paths(obs_path: str | Path) -> tuple[Path, Path]:
    """(alerts.jsonl, evidence.jsonl) paths belonging to an observation parquet."""
    p = Path(obs_path)
    return p.with_name(p.stem + ".alerts.jsonl"), p.with_name(p.stem + ".evidence.jsonl")


def write_bundle(
    obs_path: str | Path, result: ReplayResult, lake_dir: str | Path | None = None
) -> tuple[Path, Path]:
    """Persist a replay's alerts and (final-version) evidence; returns the two jsonl paths."""
    alerts_p, ev_p = bundle_paths(obs_path)
    evidence = sorted(result.evidence.values(), key=lambda e: (e.window_end, e.id))
    alerts_p.write_text("".join(a.model_dump_json() + "\n" for a in result.alerts))
    ev_p.write_text("".join(e.model_dump_json() + "\n" for e in evidence))
    lake = ParquetLake(lake_dir or Path(obs_path).parent / "lake")
    lake.append("alerts", result.alerts)
    lake.append("evidence", evidence)
    return alerts_p, ev_p


def read_bundle(obs_path: str | Path) -> tuple[list[Alert], list[Evidence]]:
    alerts_p, ev_p = bundle_paths(obs_path)

    def lines(p: Path) -> Sequence[str]:
        return [ln for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []

    return (
        [Alert.model_validate_json(ln) for ln in lines(alerts_p)],
        [Evidence.model_validate_json(ln) for ln in lines(ev_p)],
    )
