"""IODA (Georgia Tech) raw internet-outage signals -> `outage_signal` GeoEvents (ADR-007 section 1).

No key. One event per entity x datasource x time bin. `value` = observed / baseline, where the
baseline is the trailing median of the same series over `baseline_h` computed here: a plain
statistic, not detection (detection is task 021b). Position = entity centroid from
config/ioda_entities.yaml (country level, not ASN geolocation).
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from heimdall.config import CONFIG_DIR, Settings
from heimdall.geo import h3_cell
from heimdall.ingest.poll import PollConnector, Unit, num
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

DEFAULT_DATASOURCES = ("bgp", "ping-slash24", "merit-nt")
MIN_BASELINE_POINTS = 24  # need at least this many non-null trailing points for a baseline


@dataclass(frozen=True)
class IodaEntity:
    type: str  # country | region
    code: str
    name: str
    lat: float
    lon: float
    aoi: str | None = None


def load_entities(path: str | None = None) -> list[IodaEntity]:
    from pathlib import Path

    p = Path(path) if path else CONFIG_DIR / "ioda_entities.yaml"
    data = yaml.safe_load(p.read_text()) if p.exists() else None
    raw = data.get("entities", {}) if isinstance(data, dict) else {}
    out = []
    for code, e in raw.items():
        out.append(
            IodaEntity(
                type=str(e.get("type", "country")),
                code=str(code),
                name=str(e.get("name", code)),
                lat=float(e["lat"]),
                lon=float(e["lon"]),
                aoi=e.get("aoi"),
            )
        )
    return out


def parse_ioda_signals(
    payload: Mapping[str, Any],
    entity: IodaEntity,
    *,
    datasource: str | None = None,
    baseline_h: int = 168,
    emit_h: float | None = None,
) -> tuple[list[GeoEvent], int]:
    """(events, n_dropped). `emit_h` limits output to the last N hours of each series.

    Null points and bins without enough trailing history are skipped; negative counts are dropped.
    """
    data = payload.get("data")
    events: list[GeoEvent] = []
    dropped = 0
    for group in data if isinstance(data, list) else []:
        for s in group if isinstance(group, list) else []:
            if not isinstance(s, Mapping):
                dropped += 1
                continue
            ds = str(s.get("datasource", "")) if datasource is None else datasource
            t0, step, values = num(s.get("from")), num(s.get("step")), s.get("values")
            if t0 is None or step is None or step <= 0 or not isinstance(values, list):
                dropped += 1
                continue
            evs, bad = _series_events(entity, ds, int(t0), int(step), values, baseline_h, emit_h)
            events.extend(evs)
            dropped += bad
    return events, dropped


def _series_events(
    ent: IodaEntity,
    ds: str,
    t0: int,
    step: int,
    values: list[Any],
    baseline_h: int,
    emit_h: float | None,
) -> tuple[list[GeoEvent], int]:
    window = max(1, baseline_h * 3600 // step)
    first_emit = 0 if emit_h is None else max(0, len(values) - int(emit_h * 3600 // step))
    events: list[GeoEvent] = []
    dropped = 0
    for i in range(first_emit, len(values)):
        obs = num(values[i])
        if obs is None:
            continue
        if obs < 0:
            dropped += 1
            continue
        hist = [v for v in (num(x) for x in values[max(0, i - window) : i]) if v is not None]
        if len(hist) < MIN_BASELINE_POINTS:
            continue
        base = statistics.median(hist)
        if base <= 0:
            continue
        ts = datetime.fromtimestamp(t0 + i * step, tz=UTC)
        events.append(
            GeoEvent(
                id=f"ioda|{ent.type}|{ent.code}|{ds}|{t0 + i * step}",
                ts=ts,
                source="ioda",
                domain=Domain.CYBER,
                kind="outage_signal",
                lat=ent.lat,
                lon=ent.lon,
                valid_from=ts,
                valid_to=ts + timedelta(seconds=step),
                title=f"{ent.name} {ds}",
                value=obs / base,
                attrs={
                    "role": "signal",
                    "entity_type": ent.type,
                    "entity_code": ent.code,
                    "datasource": ds,
                    "observed": obs,
                    "baseline": base,
                    "level": ent.type,
                    "aoi": ent.aoi,
                },
                h3_r4=h3_cell(ent.lat, ent.lon, 4),
                h3_r6=h3_cell(ent.lat, ent.lon, 6),
            )
        )
    return events, dropped


class IodaConnector(PollConnector):
    domain = Domain.CYBER

    def __init__(
        self,
        name: str,
        base_url: str,
        entities: Sequence[IodaEntity],
        *,
        datasources: Sequence[str] = DEFAULT_DATASOURCES,
        baseline_h: int = 168,
        emit_h: float = 24.0,
        **kw: Any,
    ) -> None:
        super().__init__(name, **kw)
        self.base_url = base_url.rstrip("/")
        self.entities = list(entities)
        self.datasources = list(datasources)
        self.baseline_h, self.emit_h = baseline_h, emit_h

    def units(self) -> list[Unit]:
        return [self._unit(e, ds) for e in self.entities for ds in self.datasources]

    def _unit(self, ent: IodaEntity, ds: str) -> Unit:
        async def run() -> tuple[list[GeoEvent], int]:
            until = self._now()
            frm = until - timedelta(hours=self.baseline_h + self.emit_h)
            resp = await self.client.get(
                f"{self.base_url}/signals/raw/{ent.type}/{ent.code}",
                params={
                    "datasource": ds,
                    "from": int(frm.timestamp()),
                    "until": int(until.timestamp()),
                },
            )
            return parse_ioda_signals(
                resp.json(), ent, datasource=ds, baseline_h=self.baseline_h, emit_h=self.emit_h
            )

        return run


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return IodaConnector(
        key,
        str(cfg["base"]),
        load_entities(cfg.get("entities_file")),
        datasources=list(cfg.get("datasources", DEFAULT_DATASOURCES)),
        baseline_h=int(cfg.get("baseline_h", 168)),
        emit_h=float(cfg.get("emit_h", 24)),
        poll_s=float(cfg.get("poll_s", 600)),
        rps=float(cfg.get("rps", 1)),
        reliability=SourceReliability(cfg.get("reliability", "B")),
        expected_latency_s=int(cfg.get("latency_s", 600)),
    )
