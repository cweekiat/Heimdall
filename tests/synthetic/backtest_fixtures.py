"""Synthetic Baltic backtest day: readsb trace files + a GPSJam-format day CSV.

Regenerate the committed fixtures (deterministic, byte-identical) with:
    uv run python -m tests.synthetic.backtest_fixtures

Scenario (2026-09-01, 10:00-11:30 UTC, provider "history", H3 r4 cells in the Baltic AOI):
    A  jam      24 aircraft, 10 degraded (NIC 0)   GPSJam red      -> should alert
    B  weak     36 aircraft,  2 degraded           GPSJam yellow   -> alerts only at ratio 0.05
    C  sparse    8 aircraft,  5 degraded           GPSJam red      -> only min_aircraft 3 / sparse
    D  faulty   20 aircraft,  1 bad transponder    GPSJam green    -> must never alert
    E  clean    16 aircraft                        GPSJam green
    F  mlat      8 MLAT-only aircraft              (no GPSJam row) -> cannot judge
    G  -        no traffic in the traces           GPSJam red      -> coverage miss
    H  -        no traffic                         GPSJam 1/1      -> gray at min 3
    X  -        outside the AOI                    GPSJam red      -> filtered out
One trace file is gzip-compressed and one writes the readsb detail object only every 3rd row.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from heimdall.backtest.traces import to_trace
from heimdall.geo import h3_cell
from heimdall.schemas import AircraftObs
from tests.synthetic.generators import jammed_cell

DATE = "2026-09-01"
DAY = datetime(2026, 9, 1, tzinfo=UTC)
T_START = DAY + timedelta(hours=10)
MINUTES = 90.0
DT_S = 20.0
ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "backtest"

CELLS = {
    "A": (55.0, 20.0),
    "B": (56.5, 18.5),
    "C": (57.5, 22.0),
    "D": (55.5, 23.0),
    "E": (56.8, 20.8),
    "F": (54.8, 18.0),
    "G": (57.0, 17.0),
    "H": (56.2, 21.8),
    "X": (33.0, 35.5),
}
# (letter, n_aircraft, n_jammed, src_type, seed)
TRAFFIC = [
    ("A", 24, 10, "adsb_icao", 11),
    ("B", 36, 2, "adsb_icao", 5),
    ("C", 8, 5, "adsb_icao", 1),
    ("D", 20, 1, "adsb_icao", 14),
    ("E", 16, 0, "adsb_icao", 15),
    ("F", 8, 0, "mlat", 16),
]
GPSJAM = {  # letter -> (good, bad) for the day
    "A": (14, 10),
    "B": (34, 2),
    "C": (3, 5),
    "D": (19, 1),
    "E": (16, 0),
    "G": (10, 6),
    "H": (1, 1),
    "X": (30, 20),
}
REFS = ["2026-09-01T10:40:00Z  # synthetic NOTAM issue time for cell A"]


def cell(letter: str) -> str:
    return h3_cell(*CELLS[letter], 4)


def scenario() -> list[AircraftObs]:
    out: list[AircraftObs] = []
    for letter, n, n_jam, src, seed in TRAFFIC:
        lat, lon = CELLS[letter]
        out += jammed_cell(
            start=T_START,
            lat=lat,
            lon=lon,
            n_aircraft=n,
            n_jammed=n_jam,
            minutes=MINUTES,
            dt_s=DT_S,
            providers=("history",),
            src_type=src,
            seed=seed,
            hex_prefix=letter.lower(),
        )
    return out


def trace_payloads() -> dict[str, dict[str, object]]:
    by_hex: dict[str, list[AircraftObs]] = defaultdict(list)
    for o in scenario():
        by_hex[o.hex].append(o)
    hexes = sorted(by_hex)
    sparse_detail = hexes[1]  # one file writes the detail object only every 3rd row
    return {h: to_trace(by_hex[h], every_detail=3 if h == sparse_detail else 1) for h in hexes}


def gpsjam_csv() -> str:
    lines = ["hex,count_good_aircraft,count_bad_aircraft"]
    for letter in sorted(GPSJAM):
        good, bad = GPSJAM[letter]
        lines.append(f"{cell(letter)},{good},{bad}")
    return "\n".join(lines) + "\n"


def write(root: Path = ROOT) -> list[Path]:
    """Write traces/<hex[-2:]>/trace_full_<hex>.json (+ one gzipped), gpsjam CSV and refs."""
    written: list[Path] = []
    payloads = trace_payloads()
    gz_hex = sorted(payloads)[0]
    for h, payload in payloads.items():
        d = root / "traces" / h[-2:]
        d.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        p = d / f"trace_full_{h}.json"
        # the archives gzip trace files without renaming them; mtime=0 keeps bytes stable
        p.write_bytes(gzip.compress(data, mtime=0) if h == gz_hex else data)
        written.append(p)
    g = root / f"gpsjam-{DATE}-h3_4.csv"
    g.write_text(gpsjam_csv())
    r = root / "refs.txt"
    r.write_text("\n".join(REFS) + "\n")
    return [*written, g, r]


if __name__ == "__main__":
    for p in write():
        print(p.relative_to(ROOT.parent.parent.parent))
