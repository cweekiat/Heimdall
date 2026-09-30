"""Compare Heimdall's GNSS day map with a GPSJam CSV for the same UTC day (task 031c).

Usage:
  uv run python scripts/gnss_validate.py --date 2026-09-30 --gpsjam path/to/2026-09-30-h3_4.csv \
      [--lake data/lake] [--min-aircraft 5] [--out docs/reports/2026-09-30-gnss-map-validation.md]
Our side is read from lake/gnss_daily (or computed from the lake when not written yet).
Nothing is downloaded: fetch the GPSJam CSV yourself (gpsjam.org/data/<date>-h3_4.csv).
"""

from __future__ import annotations

import argparse
import csv
import gzip
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from heimdall.adapters.gnss_lake import GnssLake
from heimdall.config import settings
from heimdall.gnss_map import compare, gpsjam_rows
from heimdall.gnss_service import LakeGnssService


def _read_csv(p: Path) -> list[dict[str, str]]:
    raw = p.read_bytes()
    text = gzip.decompress(raw).decode() if raw[:2] == b"\x1f\x8b" else raw.decode()
    return list(csv.DictReader(text.splitlines()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--gpsjam", required=True, type=Path)
    ap.add_argument("--lake", default="data/lake")
    ap.add_argument("--min-aircraft", type=int, default=5)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    d = date.fromisoformat(a.date)
    lake = GnssLake(a.lake)
    ours = lake.read_day(a.date)
    if ours is None:
        svc = LakeGnssService(lake, settings().detectors, [])
        start = datetime(d.year, d.month, d.day, tzinfo=UTC)
        ours = svc.compute(start, start + timedelta(days=1), a.date)
    res = compare(ours.cells, gpsjam_rows(_read_csv(a.gpsjam)), a.min_aircraft)
    classes = ("green", "yellow", "red")
    lines = [
        f"# GNSS map validation vs GPSJam, {a.date}",
        "",
        f"- Cells compared (both sides >= {a.min_aircraft} aircraft): **{res.n_both}**",
        f"- Class agreement: **{res.agreement:.1%}** (target >= 90 %)",
        f"- Our day: {ours.hours_with_data}/24 hours of data"
        + (" (incomplete)" if ours.incomplete else ""),
        "",
        "| ours \\ GPSJam | " + " | ".join(classes) + " |",
        "|---|---|---|---|",
    ]
    for o in classes:
        row = [str(res.confusion.get((o, t), 0)) for t in classes]  # type: ignore[arg-type]
        lines.append(f"| {o} | " + " | ".join(row) + " |")
    lines += ["", "Largest disagreements (h3, ours %, GPSJam %):", ""]
    lines += [f"- {h} {p1} vs {p2}" for h, p1, p2 in res.worst]
    md = "\n".join(lines) + "\n"
    if a.out:
        a.out.write_text(md)
    print(md)


if __name__ == "__main__":
    main()
