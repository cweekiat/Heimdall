"""Military bases from OpenStreetMap via Overpass (task 049): a reference layer, not detections.

One weekly GET to overpass-api.de (host approved by the user 2026-10-03) for named features tagged
`military=airfield|naval_base|base` worldwide (~5,700 on 2026-10-03, ~50 s server time), written
to `data/ref/military_bases.json` as a compact list. Data © OpenStreetMap contributors, ODbL
(attribution in About and on the layer). Names are community text: displayed only.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from heimdall.config import Settings
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.poll import PollConnector, Unit
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

DEFAULT_PATH = Path("data/ref/military_bases.json")
# Tab-separated CSV keeps the answer small: the JSON form (~6 MB) is cut off by the egress proxy.
CSV_COLS = ("::type", "::id", "::lat", "::lon", "military", '"name:en"', "name", "operator", "icao")
QUERY = (
    f'[out:csv({",".join(CSV_COLS)};true;"\\t")][timeout:180];'
    'nwr["military"~"^(airfield|naval_base|base)$"]["name"];out center;'
)
KINDS = {"airfield": "Air base", "naval_base": "Naval base", "base": "Military base"}
MAX_BYTES = 40 << 20


def _clean(v: object, n: int = 80) -> str:
    return "".join(ch for ch in str(v or "") if ch.isprintable()).strip()[:n]


def parse_csv(text: str) -> list[dict[str, Any]]:
    """Overpass tab-separated answer (header row first) -> the same rows as `parse`."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("@type"):
        return []
    els = []
    for ln in lines[1:]:
        f = ln.split("\t")
        if len(f) < 9:
            continue
        typ, oid, lat, lon, mil, name_en, name, op, icao = f[:9]
        els.append(
            {
                "type": typ,
                "id": oid,
                "lat": lat,
                "lon": lon,
                "tags": {
                    "military": mil,
                    "name:en": name_en,
                    "name": name,
                    "operator": op,
                    "icao": icao,
                },
            }
        )
    return parse({"elements": els})


def parse(payload: Any) -> list[dict[str, Any]]:
    """Overpass JSON -> [{id, name, kind, lat, lon, operator, iata, icao}]."""
    els = payload.get("elements") if isinstance(payload, Mapping) else None
    out: list[dict[str, Any]] = []
    for e in els if isinstance(els, list) else []:
        if not isinstance(e, Mapping):
            continue
        raw_tags, raw_c = e.get("tags"), e.get("center")
        tags: Mapping[str, Any] = raw_tags if isinstance(raw_tags, Mapping) else {}
        c: Mapping[str, Any] = raw_c if isinstance(raw_c, Mapping) else e
        try:
            lat, lon = float(c["lat"]), float(c["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        kind = str(tags.get("military", ""))
        name = _clean(tags.get("name:en") or tags.get("name"))
        if kind not in KINDS or not name or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        out.append(
            {
                "id": f"{str(e.get('type', 'x'))[0]}{e.get('id')}",
                "name": name,
                "kind": kind,
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "operator": _clean(tags.get("operator"), 60) or None,
                "icao": _clean(tags.get("icao"), 8) or None,
            }
        )
    return out


_CACHE: dict[Path, tuple[float, list[dict[str, Any]]]] = {}


def bases(path: Path | None = None) -> list[dict[str, Any]]:
    """Cached by file mtime; empty if not downloaded yet ($HEIMDALL_BASES in tests)."""
    path = path or Path(os.environ.get("HEIMDALL_BASES") or DEFAULT_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    hit = _CACHE.get(path)
    if hit is None or hit[0] != mtime:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return hit[1] if hit else []
        hit = _CACHE[path] = (mtime, data if isinstance(data, list) else [])
    return hit[1]


def near(lat: float, lon: float, km: float, items: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Bases within `km` (cheap degree box first)."""
    dlat = km / 111.0
    dlon = km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return [
        b
        for b in items
        if abs(b["lat"] - lat) <= dlat and abs(((b["lon"] - lon + 540) % 360) - 180) <= dlon
    ]


class OsmBasesConnector(PollConnector):
    domain = Domain.AIR

    def __init__(self, name: str, url: str, *, path: Path = DEFAULT_PATH, **kw: Any) -> None:
        super().__init__(name, **kw)
        self.url, self.path = url, path

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            r = await self.client.get(self.url, params={"data": QUERY})
            if len(r.content) > MAX_BYTES:
                raise ValueError("overpass answer too large")
            if r.text.lstrip().startswith("<"):
                raise ValueError("overpass busy (HTML error page)")
            rows = parse_csv(r.text)
            if len(rows) < 1000:  # partial or failed query: keep the previous file
                raise ValueError("overpass: too few bases")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, separators=(",", ":")))
            tmp.replace(self.path)
            self.records_hour.add(len(rows))
            return [], 0

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return OsmBasesConnector(
        key,
        str(cfg["base"]),
        path=Path(str(cfg.get("path", DEFAULT_PATH))),
        poll_s=float(cfg.get("poll_s", 604800)),
        rps=float(cfg.get("rps", 0.05)),
        # one heavy query a week: long server time, a single retry (Overpass fair use)
        client=RateLimitedClient(
            key, rps=float(cfg.get("rps", 0.05)), timeout=240.0, max_retries=1
        ),
        reliability=SourceReliability(cfg.get("reliability", "C")),
        expected_latency_s=int(cfg.get("latency_s", 604800)),
    )
