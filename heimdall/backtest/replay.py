"""Replay recorded observations through `pipeline.tick` at a fixed step (backtest harness).

The replay uses an in-memory `ReplayStore` (a MemoryStore with a time index, evidence index and
DuckDB-like upsert semantics for metrics) so a full day of ticks stays fast and every result is
reproducible: the same observations, settings and step give byte-identical Evidence/Alert ids.
No wall clock is read anywhere; `now` is the simulated tick time.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from heimdall.adapters.memory_store import MemoryStore
from heimdall.config import Settings
from heimdall.detect import jamming
from heimdall.geo import BBox, Ident, obs_match, within_radius_nm
from heimdall.pipeline import tick
from heimdall.schemas import AircraftObs, Alert, AlertKind, Evidence


class ReplayStore(MemoryStore):
    """MemoryStore tuned for replay: bisect time index for `aircraft_window`, (cell, kind) index
    for `evidence_history`, and `record_metric` as an upsert on (key, metric, ts) like DuckDB."""

    def __init__(self) -> None:
        super().__init__()
        self._ts: list[datetime] = []
        self._ev_idx: dict[tuple[str, str], dict[str, Evidence]] = defaultdict(dict)
        self._metric: dict[tuple[str, str], dict[datetime, float]] = defaultdict(dict)

    def insert_aircraft(self, rows: Sequence[AircraftObs]) -> int:
        self.aircraft.extend(rows)
        self.aircraft.sort(key=lambda o: (o.ts, o.hex, o.provider))
        self._ts = [o.ts for o in self.aircraft]
        return len(rows)

    def aircraft_window(
        self,
        start: datetime,
        end: datetime,
        h3_r4: Sequence[str] | None = None,
        *,
        bbox: BBox | None = None,
        ident: Ident | None = None,
    ) -> list[AircraftObs]:
        i, j = bisect.bisect_left(self._ts, start), bisect.bisect_left(self._ts, end)
        rows = self.aircraft[i:j]
        return [
            r
            for r in rows
            if (h3_r4 is None or r.h3_r4 in h3_r4) and obs_match(r, bbox, ident, "hex")
        ]

    def upsert_evidence(self, ev: Evidence) -> None:
        super().upsert_evidence(ev)
        self._ev_idx[(ev.h3_cell or "", ev.kind.value)][ev.id] = ev

    def evidence_history(self, h3_cell: str, kind: str, since: datetime) -> list[Evidence]:
        evs = self._ev_idx.get((h3_cell, kind), {}).values()
        return sorted((e for e in evs if e.window_end >= since), key=lambda e: e.window_end)

    def record_metric(self, key: str, metric: str, ts: datetime, value: float) -> None:
        self._metric[(key, metric)][ts] = value

    def metric_history(
        self, key: str, metric: str, since: datetime
    ) -> list[tuple[datetime, float]]:
        return sorted((t, v) for t, v in self._metric.get((key, metric), {}).items() if t >= since)


@dataclass
class CellCoverage:
    """Per-cell jamming coverage over the replay (quality = true ADS-B with NIC/NACp)."""

    cell: str
    windows: int = 0  # ticks in which the cell had >= 1 quality aircraft
    totals: list[int] = field(default_factory=list)  # quality aircraft per such window
    max_ratio: float = 0.0


@dataclass
class ReplayResult:
    aoi: str
    start: datetime
    end: datetime
    step: timedelta
    ticks: int = 0
    n_obs: int = 0
    evidence: dict[str, Evidence] = field(default_factory=dict)  # final version per id
    alerts: list[Alert] = field(default_factory=list)
    raised_at: dict[str, datetime] = field(default_factory=dict)  # alert id -> first tick seen
    coverage: dict[str, CellCoverage] = field(default_factory=dict)
    detectors: dict[str, dict[str, Any]] = field(default_factory=dict)

    def jamming_evidence(self) -> list[Evidence]:
        return [e for e in self.evidence.values() if e.kind == AlertKind.GNSS_JAMMING]

    def jamming_alerts(self) -> list[Alert]:
        return [a for a in self.alerts if a.kind == AlertKind.GNSS_JAMMING]


def tick_times(start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    """Tick instants start+step, start+2*step, ... <= end (each closes the window ending there)."""
    out, t = [], start + step
    while t <= end:
        out.append(t)
        t += step
    return out


def replay(
    obs: Iterable[AircraftObs],
    settings: Settings,
    aoi: str,
    start: datetime,
    end: datetime,
    *,
    step_min: float = 5.0,
    detectors: Mapping[str, dict[str, Any]] | None = None,
    playbook: Mapping[str, Any] | None = None,
    source_grades: Mapping[str, str] | None = None,
    collect_coverage: bool = False,
) -> ReplayResult:
    """Replay `obs` in time order through `pipeline.tick` for one AOI, every `step_min` minutes.

    `detectors`, if given, is the complete *effective* detector config for the AOI (threshold
    sweeps start from `settings.detectors_for(aoi)`), so the AOI's own overrides are not
    applied again; otherwise the pipeline merges them as in production. Observations outside the
    AOI radius are dropped up front. `collect_coverage` additionally computes per-cell
    quality-aircraft totals for every tick (one extra `jamming.cell_stats` per tick).
    """
    a = settings.aoi(aoi)
    if detectors is not None:  # an explicit *effective* config: AOI overrides already applied
        a = a.model_copy(update={"detectors": {}})
    det = {k: dict(v) for k, v in (settings.detectors if detectors is None else detectors).items()}
    s = settings.model_copy(update={"aois": [a], "detectors": det})
    store = ReplayStore()
    rows = [o for o in obs if within_radius_nm(o.lat, o.lon, a.lat, a.lon, a.radius_nm)]
    store.insert_aircraft(rows)
    step = timedelta(minutes=step_min)
    res = ReplayResult(
        aoi=aoi, start=start, end=end, step=step, n_obs=len(rows), detectors=s.detectors_for(a)
    )
    jcfg = res.detectors.get("jamming", {})
    jwin = timedelta(minutes=float(jcfg.get("window_min", 30)))
    for now in tick_times(start, end, step):
        r = tick(store, s, now, playbook=playbook or {}, source_grades=source_grades or {})
        res.ticks += 1
        for e in r.evidence:
            res.evidence[e.id] = e
        for al in r.alerts:
            res.raised_at.setdefault(al.id, now)  # when an analyst could first have seen it
        if collect_coverage:
            window = store.aircraft_window(now - jwin - step, now)
            for cell, st in jamming.cell_stats(window, jcfg, now).items():
                if st.total == 0:
                    continue
                cov = res.coverage.setdefault(cell, CellCoverage(cell))
                cov.windows += 1
                cov.totals.append(st.total)
                cov.max_ratio = max(cov.max_ratio, st.ratio)
    res.alerts = sorted(store.alerts.values(), key=lambda x: (x.first_seen, x.id))
    return res
