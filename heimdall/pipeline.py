"""Pipeline tick: Store window -> detectors per AOI -> persistence -> metrics -> fusion -> Store.

Orchestration only; all maths lives in heimdall/detect and heimdall/fuse. Talks to infrastructure
exclusively through the `Store` port.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from heimdall.config import AOI, Settings
from heimdall.detect import jamming, orbit, spoofing
from heimdall.detect.common import Baseline, align_window
from heimdall.fuse.admiralty import grades_from_sources
from heimdall.fuse.fusion import fuse
from heimdall.geo import within_radius_nm
from heimdall.ports import Store
from heimdall.schemas import AircraftObs, Alert, AlertKind, AlertStatus, Evidence

ENTITY_PERSISTENCE = {AlertKind.MIL_ORBIT}  # persistence tracked per aircraft, else per cell


@dataclass
class TickResult:
    now: datetime
    n_obs: int = 0
    evidence: list[Evidence] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    metrics: int = 0


def _baseline(store: Store, now: datetime, days: float, exclude_after: datetime) -> Baseline:
    since = now - timedelta(days=days)

    def fn(key: str, metric: str) -> list[float]:
        return [v for ts, v in store.metric_history(key, metric, since) if ts < exclude_after]

    return fn


def _persistence(store: Store, ev: Evidence, step: timedelta) -> int:
    """1 + persistence of the same kind (and cell; and entity for orbits) in the previous step."""
    if ev.h3_cell is None:
        return 1
    lo, hi = ev.window_end - step * 1.5, ev.window_end - step * 0.5
    prev = [
        h
        for h in store.evidence_history(ev.h3_cell, str(ev.kind), ev.window_end - step * 2)
        if h.id != ev.id and lo <= h.window_end <= hi
    ]
    if ev.kind in ENTITY_PERSISTENCE:
        prev = [h for h in prev if set(h.entities) & set(ev.entities)]
    return 1 + max((h.persistence for h in prev), default=0)


def _active_jam_cells(
    store: Store, cells: Sequence[str], win_end: datetime, step: timedelta
) -> set[str]:
    out = set()
    for cell in cells:
        for h in store.evidence_history(cell, str(AlertKind.GNSS_JAMMING), win_end - step * 2):
            if win_end - step * 1.5 <= h.window_end < win_end:
                out.add(cell)
                break
    return out


def run_aoi(
    store: Store,
    aoi: AOI,
    obs: Sequence[AircraftObs],
    detectors: Mapping[str, dict[str, Any]],
    now: datetime,
    playbook: Mapping[str, Any] | None,
    grades: Mapping[str, str],
) -> TickResult:
    jcfg = dict(detectors.get("jamming", {}))
    scfg = dict(detectors.get("spoofing", {}))
    ocfg = dict(detectors.get("orbit", {}))
    fcfg = dict(detectors.get("fusion", {}))
    fcfg.setdefault("jam_proximity_km", ocfg.get("jam_proximity_km", 100))
    fcfg.setdefault("critical_min_tankers", ocfg.get("critical_min_tankers", 2))
    days = float(jcfg.get("baseline_days", 14))
    res = TickResult(now=now, n_obs=len(obs))

    j_start, j_end = align_window(now, jcfg.get("window_min", 30), jcfg.get("step_min", 5))
    o_start, o_end = align_window(now, ocfg.get("window_min", 75), ocfg.get("step_min", 5))
    step = timedelta(minutes=float(jcfg.get("step_min", 5)))

    stats = jamming.cell_stats(obs, jcfg, now)
    active = _active_jam_cells(store, sorted(stats), j_end, step)
    jam = jamming.detect(obs, jcfg, now, _baseline(store, now, days, j_start), active=active)
    spoof = spoofing.detect(obs, scfg, now, None)
    orb = orbit.detect(obs, ocfg, now, _baseline(store, now, days, o_start), jam_evidence=jam)

    evs: list[Evidence] = []
    for e in [*jam, *spoof, *orb]:
        e = e.model_copy(update={"aoi": aoi.name})
        e.persistence = _persistence(store, e, step)
        evs.append(e)

    min_ac = int(jcfg.get("min_aircraft", 5))
    for cell, st in stats.items():
        if st.total >= min_ac:
            store.record_metric(cell, jamming.METRIC, j_end, st.ratio)
            res.metrics += 1
    for cell in sorted({e.h3_cell for e in orb if e.h3_cell}):
        store.record_metric(cell, orbit.METRIC, o_end, 1.0)
        res.metrics += 1

    open_alerts = store.list_alerts(
        status=[AlertStatus.NEW, AlertStatus.CONFIRMED], aoi=aoi.name, limit=10_000
    )
    alerts = fuse(
        evs,
        open_alerts,
        now,
        fcfg,
        playbook,
        grades,
        aoi=aoi.name,
        control=aoi.role == "control",
    )
    for e in evs:
        store.upsert_evidence(e)
    for a in alerts:
        store.upsert_alert(a)
    res.evidence, res.alerts = evs, alerts
    return res


def tick(
    store: Store,
    settings: Settings,
    now: datetime,
    *,
    playbook: Mapping[str, Any] | None = None,
    source_grades: Mapping[str, str] | None = None,
) -> TickResult:
    """One detection step at `now` for every configured AOI."""
    if playbook is None or source_grades is None:
        from heimdall import config as _config

        playbook = _config.playbook() if playbook is None else playbook
        source_grades = (
            grades_from_sources(_config.sources()) if source_grades is None else source_grades
        )
    det = settings.detectors
    lookback = max(
        float(det.get(k, {}).get("window_min", d))
        for k, d in (("jamming", 30), ("spoofing", 30), ("orbit", 75))
    )
    step = float(det.get("jamming", {}).get("step_min", 5))
    obs_all = store.aircraft_window(now - timedelta(minutes=lookback + step), now)
    total = TickResult(now=now, n_obs=len(obs_all))
    for aoi in settings.aois:
        obs = [
            o for o in obs_all if within_radius_nm(o.lat, o.lon, aoi.lat, aoi.lon, aoi.radius_nm)
        ]
        r = run_aoi(store, aoi, obs, det, now, playbook, source_grades)
        total.evidence += r.evidence
        total.alerts += r.alerts
        total.metrics += r.metrics
    return total
