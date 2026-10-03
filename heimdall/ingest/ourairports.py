"""OurAirports airfield list (task 047): where a military aircraft took off from.

Source: https://davidmegginson.github.io/ourairports-data/airports.csv (public domain, ~12.7 MB,
~86k rows; host approved by the user 2026-10-03). The connector downloads it weekly to
`data/ref/airports.csv` (atomic replace) and emits no events. `nearest()` answers "which airfield
is this point at", using a 1-degree grid index built once per file version. Closed fields,
balloon ports and seaplane bases are dropped. `military` is a name heuristic (Air Base, RAF,
Naval Air Station, ...): OurAirports has no military flag. Names are community text: displayed only.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from heimdall.config import Settings
from heimdall.geo import haversine_km
from heimdall.ingest.poll import PollConnector, Unit
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

DEFAULT_PATH = Path("data/ref/airports.csv")
MAX_BYTES = 60 << 20
KEEP_TYPES = {"large_airport", "medium_airport", "small_airport", "heliport"}
MIL_RE = re.compile(
    r"\b(air base|air force base|afb|raf |naval air|air station|army airfield|army air|"
    r"airbase|base a[ée]rea|base aérienne|military|heliport \(military\)|aeronaval|"
    r"luftwaffe|fliegerhorst|marine corps|joint base|air national guard)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Airfield:
    ident: str
    name: str
    kind: str  # large_airport | medium_airport | small_airport | heliport
    lat: float
    lon: float
    country: str
    military: bool


def _clean(v: object, n: int = 80) -> str:
    s = "".join(ch for ch in str(v or "") if ch.isprintable()).strip()
    return s[:n]


def parse_csv(text: str) -> list[Airfield]:
    out: list[Airfield] = []
    for r in csv.DictReader(io.StringIO(text)):
        kind = r.get("type") or ""
        if kind not in KEEP_TYPES:
            continue
        try:
            lat, lon = float(r["latitude_deg"]), float(r["longitude_deg"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        name = _clean(r.get("name"))
        out.append(
            Airfield(
                ident=_clean(r.get("icao_code") or r.get("ident"), 12),
                name=name,
                kind=kind,
                lat=lat,
                lon=lon,
                country=_clean(r.get("iso_country"), 4),
                military=bool(MIL_RE.search(f"{name} {r.get('keywords') or ''}")),
            )
        )
    return out


class AirfieldIndex:
    def __init__(self, fields: Sequence[Airfield]) -> None:
        self.n = len(fields)
        self.by_ident: dict[str, Airfield] = {f.ident.upper(): f for f in fields if f.ident}
        self.grid: dict[tuple[int, int], list[Airfield]] = {}
        for f in fields:
            self.grid.setdefault((math.floor(f.lat), math.floor(f.lon)), []).append(f)

    def nearest(
        self, lat: float, lon: float, max_km: float = 15.0, heliports: bool = True
    ) -> tuple[Airfield, float] | None:
        """Closest airfield within `max_km`; larger fields win ties within 2 km (a runway's
        length), so a base is not reported as the heliport next to it."""
        best: tuple[float, Airfield] | None = None
        rank = {"large_airport": 0, "medium_airport": 1, "small_airport": 2, "heliport": 3}
        span = max(1, math.ceil(max_km / 100))
        cy, cx = math.floor(lat), math.floor(lon)
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                gx = ((cx + dx + 180) % 360) - 180
                for f in self.grid.get((cy + dy, gx), ()):
                    if not heliports and f.kind == "heliport":
                        continue
                    d = haversine_km(lat, lon, f.lat, f.lon)
                    if d > max_km:
                        continue
                    key = d + 2.0 * rank[f.kind]
                    if best is None or key < best[0]:
                        best = (key, f)
        if best is None:
            return None
        f = best[1]
        return f, haversine_km(lat, lon, f.lat, f.lon)


_CACHE: dict[Path, tuple[float, AirfieldIndex]] = {}


def index(path: Path | None = None) -> AirfieldIndex:
    """Parsed index, rebuilt only when the file changes; empty if not downloaded yet.
    Default path: $HEIMDALL_AIRPORTS (tests) or data/ref/airports.csv."""
    path = path or Path(os.environ.get("HEIMDALL_AIRPORTS") or DEFAULT_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return AirfieldIndex([])
    hit = _CACHE.get(path)
    if hit is None or hit[0] != mtime:
        try:
            idx = AirfieldIndex(parse_csv(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            return hit[1] if hit else AirfieldIndex([])
        hit = _CACHE[path] = (mtime, idx)
    return hit[1]


class OurAirportsConnector(PollConnector):
    domain = Domain.AIR

    def __init__(self, name: str, url: str, *, path: Path = DEFAULT_PATH, **kw: Any) -> None:
        super().__init__(name, **kw)
        self.url, self.path = url, path

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            r = await self.client.get(self.url)
            body = r.content
            if len(body) > MAX_BYTES:
                raise ValueError("airports.csv too large")
            text = body.decode("utf-8", errors="replace")
            n = len(parse_csv(text))
            if n < 10_000:  # truncated or not the CSV we expect: keep the previous file
                raise ValueError("airports.csv: too few rows")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
            self.records_hour.add(n)
            return [], 0

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return OurAirportsConnector(
        key,
        str(cfg["base"]),
        path=Path(str(cfg.get("path", DEFAULT_PATH))),
        poll_s=float(cfg.get("poll_s", 604800)),
        rps=float(cfg.get("rps", 0.1)),
        reliability=SourceReliability(cfg.get("reliability", "B")),
        expected_latency_s=int(cfg.get("latency_s", 604800)),
    )
