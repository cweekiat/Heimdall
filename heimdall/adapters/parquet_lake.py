"""Parquet lake: append-only, hive-partitioned by UTC date/hour. Lake is the source of truth."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel


def _row_ts(row: BaseModel) -> datetime:
    ts = getattr(row, "ts", None) or getattr(row, "created_at", None)
    if not isinstance(ts, datetime):
        return datetime.now(UTC)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _flatten(v: Any) -> Any:
    # nested dict/list columns are stored as JSON strings to keep the schema stable
    return json.dumps(v, default=str) if isinstance(v, dict | list) else v


class ParquetLake:
    def __init__(self, lake_dir: str | Path) -> None:
        self.root = Path(lake_dir)

    def append(self, dataset: str, rows: Sequence[BaseModel]) -> int:
        if not rows:
            return 0
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            ts = _row_ts(r).astimezone(UTC)
            rec = {k: _flatten(v) for k, v in r.model_dump().items()}
            buckets.setdefault((ts.strftime("%Y-%m-%d"), ts.strftime("%H")), []).append(rec)
        for (date, hour), recs in buckets.items():
            d = self.root / dataset / f"date={date}" / f"hour={hour}"
            d.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pylist(recs)
            tmp = d / f".part-{uuid.uuid4().hex}.tmp"
            pq.write_table(table, tmp, compression="zstd")
            tmp.rename(d / f"part-{uuid.uuid4().hex}.parquet")
        return len(rows)

    def paths(self, dataset: str, start: datetime, end: datetime) -> list[str]:
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        out: list[str] = []
        t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        end_u = end.astimezone(UTC)
        while t <= end_u:
            d = self.root / dataset / f"date={t:%Y-%m-%d}" / f"hour={t:%H}"
            if d.exists():
                out.extend(str(p) for p in sorted(d.glob("part-*.parquet")))
            t += timedelta(hours=1)
        return out
