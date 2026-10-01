"""plane-alert-db reference list (task 039): ~17k "interesting" airframes by ICAO hex.

Source: https://github.com/sdr-enthusiasts/plane-alert-db (`plane-alert-db.csv`, Open Database
License 1.0; attribution in the About page). The connector downloads the CSV once a day to
`data/ref/plane-alert-db.csv` (atomic replace) and emits no events; `index()` parses it (cached by
file mtime) into hex -> {registration, operator, type, icao_type, cmpg, tag1..3, category}.

Every field is community text: bounded and cleaned in `milroles.classify`, displayed, never obeyed.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from heimdall.config import Settings
from heimdall.ingest.poll import PollConnector, Unit
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

DEFAULT_PATH = Path("data/ref/plane-alert-db.csv")
MAX_BYTES = 20 << 20  # the file is ~2.5 MB; refuse anything absurd
COLUMNS = {  # CSV header (with its $ / # markers) -> our key
    "$ICAO": "hex",
    "$Registration": "registration",
    "$Operator": "operator",
    "$Type": "type",
    "$ICAO Type": "icao_type",
    "#CMPG": "cmpg",
    "$Tag 1": "tag1",
    "$#Tag 2": "tag2",
    "$#Tag 3": "tag3",
    "Category": "category",
}


def parse_csv(text: str) -> dict[str, dict[str, str]]:
    """CSV text -> {hex: entry}. Rows without a 6-hex-digit ICAO address are skipped."""
    out: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        e = {k: (row.get(src) or "").strip() for src, k in COLUMNS.items()}
        h = e.pop("hex").lower()
        if len(h) == 6 and all(c in "0123456789abcdef" for c in h):
            out[h] = e
    return out


_CACHE: dict[Path, tuple[float, dict[str, dict[str, str]]]] = {}


def index(path: Path | None = None) -> Mapping[str, Mapping[str, str]]:
    """Parsed list, re-read only when the file changes; empty if not downloaded yet.
    Default path: $HEIMDALL_PLANE_ALERT (tests) or data/ref/plane-alert-db.csv."""
    path = path or Path(os.environ.get("HEIMDALL_PLANE_ALERT") or DEFAULT_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _CACHE.get(path)
    if hit is None or hit[0] != mtime:
        try:
            hit = (mtime, parse_csv(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            return hit[1] if hit else {}
        _CACHE[path] = hit
    return hit[1]


class PlaneAlertConnector(PollConnector):
    domain = Domain.AIR

    def __init__(self, name: str, url: str, *, path: Path = DEFAULT_PATH, **kw: Any) -> None:
        super().__init__(name, **kw)
        self.url, self.path = url, path

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            r = await self.client.get(self.url)
            body = r.content
            if len(body) > MAX_BYTES:
                raise ValueError("plane-alert-db too large")
            text = body.decode("utf-8", errors="replace")
            n = len(parse_csv(text))
            if n < 1000:  # truncated or not the CSV we expect: keep the previous file
                raise ValueError("plane-alert-db: too few rows")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
            self.records_hour.add(n)
            return [], 0

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return PlaneAlertConnector(
        key,
        str(cfg["base"]),
        path=Path(str(cfg.get("path", DEFAULT_PATH))),
        poll_s=float(cfg.get("poll_s", 86400)),
        rps=float(cfg.get("rps", 0.1)),
        reliability=SourceReliability(cfg.get("reliability", "C")),
        expected_latency_s=int(cfg.get("latency_s", 86400)),
    )
