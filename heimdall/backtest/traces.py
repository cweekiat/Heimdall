"""Parser for readsb `globe_history` trace files (adsb.lol history archives) -> AircraftObs.

Format (wiedehopf/readsb README-json.md "trace jsons"): one JSON object per aircraft,
optionally gzip-compressed (the archives gzip `trace_full_<hex>.json` without renaming it)::

    {"icao": "4ca7b5", "timestamp": 1756720800.0, "r": "EI-ABC", "t": "B738", "dbFlags": 0,
     "trace": [[dt, lat, lon, alt, gs, track, flags, vrate, detail, type, alt_geom, ...], ...]}

* `dt` = seconds after the base `timestamp`; `alt` = ft, "ground" or null.
* `flags` bitfield: 1 stale position, 2 new leg, 4 vrate is geometric, 8 alt is geometric.
* `detail` = a readsb aircraft object (or null) with `nic`, `nac_p`, `sil`, `rc`, `type`,
  `flight`, `alt_geom` ... It is written only on some rows. **Assumption:** rows without a detail
  object have *unknown* NIC/NACp (they are not forward-filled), which the jamming detector ignores;
  forward-filling would turn one degraded report into minutes of "degraded" track.
* Position source type = row[9] (files from 2022 on) else `detail.type` else "other" (excluded
  from the jamming ratio, like MLAT). Geometric altitude = row[10] else `detail.alt_geom`.

`parse_trace` is pure. `read_trace_file` / `iter_trace_dir` only read local files (no network).
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from heimdall.geo import h3_cell, within_radius_nm
from heimdall.schemas import AircraftObs

PROVIDER = "history"  # -> sources.yaml key adsb_lol_history (fuse.admiralty alias)
MAX_ALT_FT = 100_000
MIN_ALT_FT = -2_000
MAX_GS_KT = 2_000.0
FLAG_STALE, FLAG_NEW_LEG, FLAG_GEOM_VRATE, FLAG_GEOM_ALT = 1, 2, 4, 8
TRACE_SUFFIXES = (".json", ".json.gz", ".gz")


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    f = _num(v)
    return None if f is None else round(f)


def _at(row: list[Any], i: int) -> Any:
    return row[i] if len(row) > i else None


def parse_trace(
    payload: dict[str, Any],
    provider: str = PROVIDER,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    area: tuple[float, float, float] | None = None,
) -> Iterator[AircraftObs]:
    """Yield AircraftObs for each plausible trace row, in file order.

    Optional filters: `start <= ts < end` and `area = (lat, lon, radius_nm)`.
    Implausible rows (lat/lon out of range, null island, |alt| or gs out of range) are skipped.
    """
    hex_ = str(payload.get("icao") or "").strip().lower()
    base = _num(payload.get("timestamp"))
    if not hex_ or base is None:
        return
    type_code = payload.get("t") or None
    registration = payload.get("r") or None
    mil = bool((_int(payload.get("dbFlags")) or 0) & 1)
    t0, t1 = (start.timestamp() if start else None), (end.timestamp() if end else None)
    for row in payload.get("trace") or []:
        if not isinstance(row, list) or len(row) < 3:
            continue
        dt, lat, lon = _num(row[0]), _num(row[1]), _num(row[2])
        if dt is None or lat is None or lon is None:
            continue
        t = base + dt
        if (t0 is not None and t < t0) or (t1 is not None and t >= t1):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or (lat == 0.0 and lon == 0.0):
            continue
        if area is not None and not within_radius_nm(lat, lon, *area):
            continue
        detail = _at(row, 8) if isinstance(_at(row, 8), dict) else {}
        flags = _int(_at(row, 6)) or 0
        raw_alt = _at(row, 3)
        on_ground = raw_alt == "ground"
        alt = None if on_ground else _int(raw_alt)
        if alt is not None and not (MIN_ALT_FT <= alt <= MAX_ALT_FT):
            continue
        gs = _num(_at(row, 4))
        if gs is not None and not (0.0 <= gs <= MAX_GS_KT):
            continue
        alt_geom = _int(_at(row, 10))
        if alt_geom is None:
            alt_geom = _int(detail.get("alt_geom"))
        alt_baro = alt
        if flags & FLAG_GEOM_ALT:
            alt_baro, alt_geom = None, alt if alt_geom is None else alt_geom
        vrate = _int(_at(row, 7))
        geom_rate = vrate if flags & FLAG_GEOM_VRATE else _int(_at(row, 11))
        baro_rate = None if flags & FLAG_GEOM_VRATE else vrate
        src = _at(row, 9) or detail.get("type") or "other"
        flight = detail.get("flight")
        yield AircraftObs(
            ts=datetime.fromtimestamp(t, UTC),
            hex=hex_,
            provider=provider,
            lat=lat,
            lon=lon,
            src_type=str(src),
            flight=flight.strip() or None if isinstance(flight, str) else None,
            type_code=type_code or detail.get("t") or None,
            registration=registration or detail.get("r") or None,
            mil=mil,
            alt_baro=alt_baro,
            alt_geom=alt_geom,
            on_ground=on_ground,
            gs=gs,
            track=_num(_at(row, 5)),
            baro_rate=baro_rate,
            geom_rate=geom_rate,
            nic=_int(detail.get("nic")),
            nac_p=_int(detail.get("nac_p")),
            sil=_int(detail.get("sil")),
            rc=_int(detail.get("rc")),
            gps_ok_before=_num(detail.get("gpsOkBefore")),
            h3_r4=h3_cell(lat, lon, 4),
            h3_r6=h3_cell(lat, lon, 6),
        )


def read_trace_file(path: str | Path) -> dict[str, Any]:
    """Load one trace JSON from a local path; gzip detected by magic bytes, not by name."""
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: trace file is not a JSON object")
    return data


def trace_files(root: str | Path) -> list[Path]:
    """All trace files under `root` (a file or directory), sorted for deterministic replay."""
    p = Path(root)
    if p.is_file():
        return [p]
    return sorted(f for f in p.rglob("*") if f.is_file() and f.name.endswith(TRACE_SUFFIXES))


def iter_trace_dir(
    root: str | Path,
    provider: str = PROVIDER,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    area: tuple[float, float, float] | None = None,
) -> Iterator[AircraftObs]:
    """Stream AircraftObs from every trace file under `root` (file order, not time order)."""
    for f in trace_files(root):
        yield from parse_trace(read_trace_file(f), provider, start=start, end=end, area=area)


def to_trace(obs: Iterable[AircraftObs], every_detail: int = 1) -> dict[str, Any]:
    """Inverse of `parse_trace` for one aircraft (used to build synthetic fixtures).

    `every_detail` = write the detail object on every n-th row only (readsb writes it sparsely).
    """
    rows = sorted(obs, key=lambda o: o.ts)
    if not rows:
        raise ValueError("no observations")
    base = rows[0].ts.timestamp()
    trace: list[list[Any]] = []
    for i, o in enumerate(rows):
        detail: dict[str, Any] | None = None
        if i % every_detail == 0:
            detail = {k: v for k, v in (("nic", o.nic), ("nac_p", o.nac_p)) if v is not None}
            detail["type"] = o.src_type
            if o.flight:
                detail["flight"] = o.flight
        alt: Any = "ground" if o.on_ground else o.alt_baro
        trace.append(
            [
                round(o.ts.timestamp() - base, 2),
                round(o.lat, 6),
                round(o.lon, 6),
                alt,
                None if o.gs is None else round(o.gs, 1),
                None if o.track is None else round(o.track, 1),
                0,
                o.baro_rate,
                detail,
                o.src_type,
                o.alt_geom,
            ]
        )
    first = rows[0]
    out: dict[str, Any] = {"icao": first.hex, "timestamp": base, "trace": trace}
    if first.type_code:
        out["t"] = first.type_code
    if first.registration:
        out["r"] = first.registration
    if first.mil:
        out["dbFlags"] = 1
    return out
