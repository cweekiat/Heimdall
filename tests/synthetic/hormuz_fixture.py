"""Faithful synthetic in adsb.lol / readsb `trace_full_*.json` format for the Hormuz replay tests.

`uv run python -m tests.synthetic.hormuz_fixture` regenerates tests/fixtures/adsblol/
hormuz_sample.json.gz: a JSON list of trace objects (the daily archive holds one object per file).
Not real data: the shape mirrors a 2025-06-17 window (jamming cell over the strait, a quiet Gulf
of Oman cell, aircraft outside the bbox), so there is no third-party licence to carry.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from heimdall.backtest.traces import to_trace
from heimdall.schemas import AircraftObs
from tests.synthetic.generators import jammed_cell, straight_flight

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "adsblol"
SAMPLE = ROOT / "hormuz_sample.json.gz"
DATE = "2025-06-17"
T0 = datetime(2025, 6, 17, 10, 0, tzinfo=UTC)
PROV = ("history",)


def payloads() -> list[dict[str, Any]]:
    obs: list[AircraftObs] = []
    for b in range(4):  # 4 x 30 min of a strait cell with 4/10 aircraft reporting NIC=NACp=0
        obs += jammed_cell(
            T0 + timedelta(minutes=30 * b),
            26.5,
            56.3,
            n_aircraft=10,
            n_jammed=4,
            dt_s=30,
            providers=PROV,
            hex_prefix=f"7{b}",
        )
    for b in range(4):  # quiet Gulf of Oman cell, healthy NIC
        obs += jammed_cell(
            T0 + timedelta(minutes=30 * b),
            24.6,
            58.6,
            n_aircraft=8,
            n_jammed=0,
            dt_s=30,
            providers=PROV,
            hex_prefix=f"8{b}",
        )
    for i, (lat, lon) in enumerate([(19.1, 72.9), (24.9, 67.1), (33.6, 73.0)]):  # outside bbox
        obs += straight_flight(f"9{i:05x}", T0, lat, lon, 90.0, 450.0, 20.0, 60.0)
    by_hex: dict[str, list[AircraftObs]] = {}
    for o in obs:
        by_hex.setdefault(o.hex, []).append(o)
    return [to_trace(rows) for _, rows in sorted(by_hex.items())]


def write(path: Path = SAMPLE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payloads(), separators=(",", ":")).encode()
    path.write_bytes(gzip.compress(raw, mtime=0))
    return path


if __name__ == "__main__":
    print(write())
