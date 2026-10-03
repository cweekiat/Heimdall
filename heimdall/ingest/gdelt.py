"""GDELT 2.0 conflict events (task 049): context layer and corroboration, never an alert on its own.

Every 15 min GDELT publishes the events machine-coded from world news in that window
(`https://data.gdeltproject.org/gdeltv2/lastupdate.txt` -> `<stamp>.export.CSV.zip`, ~40 KB zipped,
~600 events; host approved by the user 2026-10-03). Kept: CAMEO root 15 (exhibit force posture),
18 (assault), 19 (fight), 20 (unconventional mass violence), with a geocoded place below country
level. Machine coding from news is noisy (wrong places, old stories re-reported), so each event
carries its number of sources and articles, and the layer says so. Stored fields are GDELT's own
codes and numbers plus the place name and the source URL's domain; article text is never fetched.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from heimdall.config import Settings
from heimdall.ingest.poll import PollConnector, Unit
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

ROOTS = {"15": "Show of force", "18": "Assault", "19": "Fighting", "20": "Mass violence"}
CODES = {
    "150": "Show of force",
    "151": "Military alert raised",
    "152": "Police alert raised",
    "153": "Military forces mobilised",
    "154": "Military forces mobilised",
    "155": "Cyber forces mobilised",
    "180": "Assault",
    "181": "Abduction or hostage taking",
    "182": "Physical assault",
    "183": "Bombing",
    "1831": "Suicide bombing",
    "1832": "Vehicle bombing",
    "1833": "Roadside bombing",
    "184": "Human shield use",
    "185": "Assassination attempt",
    "186": "Assassination",
    "190": "Use of conventional military force",
    "191": "Blockade",
    "192": "Occupation of territory",
    "193": "Small arms fighting",
    "194": "Artillery and tank fighting",
    "195": "Aerial weapons used",
    "1951": "Precision-guided aerial munitions",
    "1952": "Remotely piloted aerial munitions",
    "196": "Ceasefire violated",
    "200": "Mass violence",
    "201": "Mass expulsion",
    "202": "Mass killings",
    "203": "Ethnic cleansing",
    "204": "Weapons of mass destruction used",
}
GEO_PRECISION = {"2": "US state", "3": "US city", "4": "city", "5": "region"}
MAX_ZIP = 5 << 20


def _f(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def _domain(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return None
    return host.removeprefix("www.")[:80] or None


def parse_export(text: str) -> tuple[list[GeoEvent], int]:
    """GDELT 2.0 events export (61 tab-separated columns) -> conflict GeoEvents."""
    out: list[GeoEvent] = []
    dropped = 0
    for r in csv.reader(io.StringIO(text), delimiter="\t"):
        if len(r) < 61:
            dropped += 1
            continue
        root, code = r[28], r[26]
        if root not in ROOTS:
            continue
        lat, lon = _f(r[56]), _f(r[57])
        if r[51] not in GEO_PRECISION or lat is None or lon is None or (lat == 0 and lon == 0):
            dropped += 1
            continue
        try:
            ts = datetime.strptime(r[59], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            dropped += 1
            continue
        label = CODES.get(code) or CODES.get(code[:3]) or ROOTS[root]
        out.append(
            GeoEvent(
                id=f"gdelt|{r[0]}",
                ts=ts,
                source="gdelt",
                domain=Domain.EVENTS,
                kind="conflict_event",
                lat=lat,
                lon=lon,
                valid_from=ts,
                valid_to=ts + timedelta(hours=24),
                title=label,
                value=_f(r[32]),  # number of sources
                url=r[60][:500] if r[60].startswith(("http://", "https://")) else None,
                attrs={
                    "role": "context",
                    "cameo": code,
                    "root": root,
                    "label": label,
                    "goldstein": _f(r[30]),
                    "mentions": int(_f(r[31]) or 0),
                    "sources": int(_f(r[32]) or 0),
                    "articles": int(_f(r[33]) or 0),
                    "actor1": r[6][:60] or None,
                    "actor2": r[16][:60] or None,
                    "place": r[52][:120] or None,
                    "country": r[53][:4] or None,
                    "precision": GEO_PRECISION[r[51]],
                    "event_day": r[1],
                    "domain": _domain(r[60]),
                },
            )
        )
    return out, dropped


def export_url(lastupdate: str) -> str | None:
    """URL of the newest events export from lastupdate.txt (https, GDELT host only)."""
    for line in lastupdate.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].endswith(".export.CSV.zip"):
            url = parts[2].replace("http://", "https://", 1)
            if urlsplit(url).hostname == "data.gdeltproject.org":
                return url
    return None


class GdeltConnector(PollConnector):
    domain = Domain.EVENTS

    def __init__(self, name: str, base_url: str, **kw: Any) -> None:
        super().__init__(name, **kw)
        self.base_url = base_url.rstrip("/")
        self.last_url: str | None = None

    def units(self) -> Sequence[Unit]:
        async def run() -> tuple[list[GeoEvent], int]:
            lu = await self.client.get(f"{self.base_url}/gdeltv2/lastupdate.txt")
            url = export_url(lu.text)
            if url is None:
                raise ValueError("gdelt: no export in lastupdate.txt")
            if url == self.last_url:
                return [], 0
            z = await self.client.get(url)
            if len(z.content) > MAX_ZIP:
                raise ValueError("gdelt export too large")
            with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
                names = [n for n in zf.namelist() if n.endswith(".export.CSV")]
                if not names:
                    raise ValueError("gdelt: zip without export")
                text = zf.read(names[0]).decode("utf-8", errors="replace")
            self.last_url = url
            return parse_export(text)

        return [run]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return GdeltConnector(
        key,
        str(cfg["base"]),
        poll_s=float(cfg.get("poll_s", 900)),
        rps=float(cfg.get("rps", 0.2)),
        reliability=SourceReliability(cfg.get("reliability", "D")),
        expected_latency_s=int(cfg.get("latency_s", 1800)),
    )
