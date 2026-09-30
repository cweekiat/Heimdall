"""Filter adsb.lol readsb trace files to a bounding box and write a replay parquet.

    uv run python scripts/filter_adsblol.py OUT.parquet [SOURCE ...] [--bbox S W N E]

SOURCE is any of: a directory of `trace_full_*.json[.gz]`, a `.tar` / `.tar.gz` archive, `-`
(a tar stream on stdin, used by fetch_adsblol_day.sh), or a `.json[.gz]` file holding one trace
object or a list of them (the test fixture). Rows outside the bbox are dropped *before* parsing so
a world-wide day stays cheap; kept rows keep their exact time, `alt_baro`, NIC and NACp
(`heimdall.backtest.traces.parse_trace` semantics). Pure file I/O, no network.

adsb.lol data is published under the ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/);
attribution: "Contains information from adsb.lol, made available under the ODbL".
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tarfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from heimdall.backtest.replay_io import write_obs_parquet
from heimdall.backtest.traces import PROVIDER, TRACE_SUFFIXES, parse_trace
from heimdall.schemas import AircraftObs

#: (lat_min, lon_min, lat_max, lon_max): circumscribes the `hormuz` AOI (26.3N 56.3E, 200 NM).
HORMUZ_BBOX = (22.9, 52.5, 29.7, 60.1)

BBox = tuple[float, float, float, float]


def _loads(raw: bytes) -> Any:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def in_bbox(lat: Any, lon: Any, bbox: BBox) -> bool:
    if isinstance(lat, bool) or isinstance(lon, bool):
        return False
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return False
    return bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]


def filter_payload(payload: dict[str, Any], bbox: BBox = HORMUZ_BBOX) -> dict[str, Any] | None:
    """Copy of a readsb trace object keeping only rows inside `bbox`; None if no row is kept.

    Rows are kept verbatim (time offset `dt`, altitude, detail object with nic/nac_p), so the
    base `timestamp` and every kept row's absolute time are unchanged.
    """
    rows = [
        r
        for r in payload.get("trace") or []
        if isinstance(r, list) and len(r) >= 3 and in_bbox(r[1], r[2], bbox)
    ]
    return {**payload, "trace": rows} if rows else None


def iter_payloads(source: str | Path) -> Iterator[dict[str, Any]]:
    """Trace objects from a dir, tar/tar.gz (or `-` = tar stream on stdin) or json[.gz] file."""
    src = str(source)
    if src == "-":
        yield from _iter_tar(tarfile.open(fileobj=sys.stdin.buffer, mode="r|*"))  # noqa: SIM115
        return
    p = Path(src)
    if p.is_dir():
        for f in sorted(x for x in p.rglob("*") if x.is_file() and x.name.endswith(TRACE_SUFFIXES)):
            data = _loads(f.read_bytes())
            yield from (data if isinstance(data, list) else [data])
    elif ".tar" in p.name:
        yield from _iter_tar(tarfile.open(p, mode="r:*"))  # noqa: SIM115
    else:
        data = _loads(p.read_bytes())
        yield from (data if isinstance(data, list) else [data])


def _iter_tar(tar: tarfile.TarFile) -> Iterator[dict[str, Any]]:
    with tar:
        for m in tar:
            if not m.isfile() or "trace_full_" not in m.name:
                continue
            fh = tar.extractfile(m)
            if fh is None:
                continue
            try:
                data = _loads(fh.read())
            except (ValueError, OSError):
                continue  # one corrupt member must not abort a day-long stream
            if isinstance(data, dict):
                yield data


def filter_sources(
    sources: Sequence[str | Path], bbox: BBox = HORMUZ_BBOX, provider: str = PROVIDER
) -> list[AircraftObs]:
    obs: list[AircraftObs] = []
    for s in sources:
        for payload in iter_payloads(s):
            kept = filter_payload(payload, bbox)
            if kept is not None:
                obs.extend(parse_trace(kept, provider))
    return obs


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", help="output parquet path, e.g. data/replay/hormuz/2025-06-17.parquet")
    ap.add_argument("sources", nargs="*", default=["-"], help="default: tar stream on stdin")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("S", "W", "N", "E"))
    ap.add_argument("--provider", default=PROVIDER)
    args = ap.parse_args(argv)
    bbox: BBox = tuple(args.bbox) if args.bbox else HORMUZ_BBOX  # type: ignore[assignment]
    obs = filter_sources(args.sources, bbox, args.provider)
    n = write_obs_parquet(args.out, obs)
    print(f"{n} rows in bbox {bbox} -> {args.out}")
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
