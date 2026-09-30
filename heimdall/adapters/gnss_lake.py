"""GNSS interference map over the Parquet lake (task 031). Infrastructure: DuckDB + pyarrow.

The hot DuckDB store keeps only `hot_retention_h` of aircraft, so 24 h maps are computed from the
lake (`aircraft/date=D/hour=H/*.parquet`). DuckDB returns per (cell, aircraft) report counts using
the same rule as `detect.jamming.degraded` (NIC < nic_below or NACp < nacp_below; neither known =
ignored); `heimdall.gnss_map.aggregate` does the rest. Finished days are written to
`gnss_daily/date=D/cells.parquet` (the lake stays the source of truth).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from heimdall.gnss_map import AcCount, GnssCell, GnssDay

TRUE_ADSB = ("adsb_icao", "adsb_icao_nt")

SQL = """
SELECT h3_r4, hex,
  count(*) FILTER (WHERE nic IS NOT NULL OR nac_p IS NOT NULL) AS known,
  count(*) FILTER (WHERE nic < ? OR nac_p < ?) AS bad,
  bool_or(coalesce(mil, false)) AS mil,
  list_sort(list_distinct(list(provider))) AS providers
FROM read_parquet(?, union_by_name = true)
WHERE NOT coalesce(on_ground, false) AND src_type IN ('adsb_icao', 'adsb_icao_nt')
  AND h3_r4 IS NOT NULL AND ts >= ?::TIMESTAMPTZ AND ts < ?::TIMESTAMPTZ
GROUP BY 1, 2
HAVING known > 0
"""


class GnssLake:
    def __init__(self, lake_dir: str | Path) -> None:
        self.root = Path(lake_dir)

    def _hour_dirs(self, start: datetime, end: datetime) -> list[Path]:
        out: list[Path] = []
        t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        while t < end:
            d = self.root / "aircraft" / f"date={t.date().isoformat()}" / f"hour={t.hour:02d}"
            if d.is_dir() and any(d.glob("*.parquet")):
                out.append(d)
            t += timedelta(hours=1)
        return out

    def hours_with_data(self, start: datetime, end: datetime) -> int:
        return len(self._hour_dirs(start, end))

    def counts(
        self, start: datetime, end: datetime, nic_below: int = 7, nacp_below: int = 8
    ) -> list[AcCount]:
        dirs = self._hour_dirs(start, end)
        if not dirs:
            return []
        files = [str(d / "*.parquet") for d in dirs]
        con = duckdb.connect()
        try:
            rows = con.execute(
                SQL,
                [nic_below, nacp_below, files, start.isoformat(), end.isoformat()],
            ).fetchall()
        finally:
            con.close()
        return [AcCount(r[0], r[1], int(r[2]), int(r[3]), bool(r[4]), tuple(r[5])) for r in rows]

    # ------------------------------------------------------------------ finished days

    def _day_file(self, day: str) -> Path:
        return self.root / "gnss_daily" / f"date={day}" / "cells.parquet"

    def has_day(self, day: str) -> bool:
        return self._day_file(day).is_file()

    def write_day(self, d: GnssDay) -> None:
        p = self._day_file(d.date)
        p.parent.mkdir(parents=True, exist_ok=True)
        t = pa.table(
            {
                "h3": [c.h3 for c in d.cells],
                "good": [c.good for c in d.cells],
                "bad": [c.bad for c in d.cells],
                "total": [c.total for c in d.cells],
                "pct": [c.pct for c in d.cells],
                "cls": [c.cls for c in d.cells],
                "mil_excluded": [c.mil_excluded for c in d.cells],
                "providers": [json.dumps(c.providers) for c in d.cells],
            }
        )
        meta = {
            "hours_with_data": str(d.hours_with_data),
            "incomplete": str(d.incomplete),
            "sources": json.dumps(d.sources),
        }
        tmp = p.with_suffix(".tmp")
        pq.write_table(t.replace_schema_metadata(meta), tmp)
        tmp.replace(p)  # atomic: readers never see a half-written day

    def read_day(self, day: str) -> GnssDay | None:
        p = self._day_file(day)
        if not p.is_file():
            return None
        t = pq.read_table(p)
        md = {k.decode(): v.decode() for k, v in (t.schema.metadata or {}).items()}
        cols = t.to_pydict()
        cells = [
            GnssCell(
                h3=cols["h3"][i],
                good=cols["good"][i],
                bad=cols["bad"][i],
                total=cols["total"][i],
                pct=cols["pct"][i],
                cls=cols["cls"][i],
                mil_excluded=cols["mil_excluded"][i],
                providers=json.loads(cols["providers"][i]),
            )
            for i in range(t.num_rows)
        ]
        return GnssDay(
            date=day,
            cells=cells,
            hours_with_data=int(md.get("hours_with_data", "24")),
            incomplete=md.get("incomplete") == "True",
            sources=json.loads(md.get("sources", "[]")),
        )

    def days(self) -> list[str]:
        base = self.root / "gnss_daily"
        if not base.is_dir():
            return []
        out = []
        for d in base.glob("date=*"):
            day = d.name.removeprefix("date=")
            try:
                date.fromisoformat(day)
            except ValueError:
                continue
            if (d / "cells.parquet").is_file():
                out.append(day)
        return sorted(out)
