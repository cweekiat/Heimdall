"""Pipeline tick: Store window -> detectors per AOI -> persistence -> metrics -> fusion -> Store.

Orchestration only; all maths lives in heimdall/detect and heimdall/fuse. Talks to infrastructure
exclusively through the `Store` port.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from heimdall import orbits
from heimdall.config import AOI, Settings
from heimdall.detect import (
    ais_gap,
    ais_spoofing,
    jamming,
    night_lights,
    orbit,
    outage,
    spoofing,
    thermal,
    warning_area,
)
from heimdall.detect.common import (
    Baseline,
    align_window,
    vessel_cell_of,
    vessel_in_window,
)
from heimdall.fuse.admiralty import grades_from_sources
from heimdall.fuse.fusion import ACTIVE, apply_evidence_update, fuse
from heimdall.geo import h3_cell, h3_center, haversine_km
from heimdall.logging import get_logger
from heimdall.ports import Store
from heimdall.schemas import (
    AircraftObs,
    Alert,
    AlertKind,
    AlertStatus,
    Evidence,
    GeoEvent,
    VesselObs,
)

ENTITY_PERSISTENCE = {AlertKind.MIL_ORBIT, AlertKind.AIS_GAP}  # per aircraft/vessel, else per cell


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


AIS_METRIC = "ais_vessels"  # distinct MMSIs per r4 cell in the AIS spoofing window
PERSIST_METRIC = "persistence_run_start"  # epoch s of the first anomalous obs of the current run
PERSIST_TICKS = "persistence_ticks"  # consecutive steps in which the subject triggered


def persistence_key(aoi: str, ev: Evidence) -> str:
    """Persistence subject: aircraft (ICAO hex) for orbits, MMSI for per-vessel AIS kinds (gap;
    spoofing A1/A4 flag `per_vessel`), else the H3 cell."""
    if ev.kind in ENTITY_PERSISTENCE and ev.entities:
        if ev.kind == AlertKind.AIS_GAP:
            return f"{aoi}|{ev.kind}|mmsi:{ev.entities[0]}"
        return f"{aoi}|{ev.kind}|hex:{ev.entities[0]}"
    if ev.kind == AlertKind.AIS_SPOOFING and ev.features.get("per_vessel") and ev.entities:
        return f"{aoi}|{ev.kind}|mmsi:{ev.entities[0]}"
    if ev.kind == AlertKind.THERMAL:
        return f"{aoi}|{ev.kind}|r6:{ev.h3_cell}"
    if ev.kind == AlertKind.OUTAGE:
        if ev.entities:
            return f"{aoi}|{ev.kind}|entity:{ev.entities[0].split(':', 1)[-1]}"
        return f"{aoi}|{ev.kind}|r4:{ev.h3_cell}"
    return f"{aoi}|{ev.kind}|cell:{ev.h3_cell}"


def _assign_persistence(
    store: Store, aoi: str, evs: Sequence[Evidence], step: timedelta
) -> list[Evidence]:
    """Persistence = min(consecutive triggered steps, 1 + floor((last obs - run start) / step)).

    Windows (30 min) overlap heavily at a 5-min step, so counting triggered *windows* would turn
    one short burst into 6 "persistent" windows. Instead each detector reports the time span of
    the observations that make the anomaly (`anomaly_first_ts`/`anomaly_last_ts`); a run starts at
    the earliest anomalous observation and is carried forward (via a metric keyed by
    `persistence_key`) as long as the subject triggers in consecutive steps. A single event shorter
    than one step can therefore never reach persistence 2, however many windows contain it; and
    the tick count keeps the ">= 2 evaluations" rule (a first tick is always persistence 1).
    """
    groups: dict[str, list[Evidence]] = {}
    for e in evs:
        groups.setdefault(persistence_key(aoi, e), []).append(e)
    out: list[Evidence] = []
    step_s = step.total_seconds()
    for key, group in groups.items():
        end = max(e.window_end for e in group)
        firsts = [e.features["anomaly_first_ts"] for e in group if "anomaly_first_ts" in e.features]
        lasts = [e.features["anomaly_last_ts"] for e in group if "anomaly_last_ts" in e.features]
        if not firsts or not lasts:
            out += [e.model_copy(update={"persistence": 1}) for e in group]
            continue
        lo, hi = end - step * 1.5, end - step * 0.5
        prev_start = [
            v for ts, v in store.metric_history(key, PERSIST_METRIC, lo) if lo <= ts <= hi
        ]
        prev_ticks = [v for ts, v in store.metric_history(key, PERSIST_TICKS, lo) if lo <= ts <= hi]
        run_start = min([*firsts, *prev_start])
        ticks = 1 + int(max(prev_ticks, default=0.0))
        span_steps = 1 + max(0, int((max(lasts) - run_start) // step_s))
        p = min(ticks, span_steps)
        store.record_metric(key, PERSIST_METRIC, end, run_start)
        store.record_metric(key, PERSIST_TICKS, end, float(ticks))
        out += [
            e.model_copy(
                update={
                    "persistence": p,
                    "features": {
                        **e.features,
                        "run_start_ts": run_start,
                        "persist_ticks": float(ticks),
                        "persist_span_steps": float(span_steps),
                    },
                }
            )
            for e in group
        ]
    return out


def _active_jam_cells(
    store: Store, aoi: str, cells: Sequence[str], win_end: datetime, step: timedelta
) -> set[str]:
    out = set()
    for cell in cells:
        for h in store.evidence_history(cell, str(AlertKind.GNSS_JAMMING), win_end - step * 2):
            if h.aoi == aoi and win_end - step * 1.5 <= h.window_end < win_end:
                out.add(cell)
                break
    return out


FIRMS_METRIC = "firms_detections"  # thermal detections per r4 cell in the detector window
NIGHT_METRIC = "night_lights_mean"  # latest Black Marble night mean radiance per r4 cell
OVERPASS_METRIC = "overpass_refresh"  # last time overpasses were (re)computed
OVERPASS_REFRESH_H = 6.0  # predictions move by seconds per day; recompute a few times a day
log = get_logger("heimdall.pipeline")


@dataclass
class P3Windows:
    """Event windows for the P3 detectors (ADR-007), shared by all AOIs of one tick."""

    thermal: Sequence[GeoEvent] = ()  # detector window + baseline history in one list
    outage: Sequence[GeoEvent] = ()
    night: Sequence[GeoEvent] = ()


def events_window(
    store: Store, kinds: Sequence[str], lookback: timedelta, now: datetime
) -> list[GeoEvent]:
    """Events of `kinds` in [now - lookback, now]. The Store has no aggregate query, so this is a
    bounded list; callers pass no more lookback than the detector needs."""
    return store.events_window(now - lookback, now + timedelta(seconds=1), list(kinds))


def _p3_windows(
    store: Store, dets: Mapping[str, Mapping[str, Mapping[str, Any]]], now: datetime
) -> P3Windows:
    def mx(det: str, key: str, default: float) -> float:
        return max(float(d.get(det, {}).get(key, default)) for d in dets.values())

    t_days = mx("thermal", "baseline_days", thermal.DEFAULTS["baseline_days"]) + (
        mx("thermal", "window_h", thermal.DEFAULTS["window_h"]) / 24 + 1
    )
    o_h = mx("outage", "window_h", outage.DEFAULTS["window_h"]) + 1
    n_days = mx("night_lights", "baseline_nights", night_lights.DEFAULTS["baseline_nights"]) + 5
    return P3Windows(
        thermal=events_window(store, ["thermal"], timedelta(days=t_days), now),
        outage=events_window(
            store, ["outage_signal", "outage_annotation"], timedelta(hours=o_h), now
        ),
        night=events_window(store, ["night_lights"], timedelta(days=n_days), now),
    )


def update_overpasses(
    store: Store, aois: Sequence[AOI], ccfg: Mapping[str, Any], now: datetime
) -> int:
    """CelesTrak post-ingest hook: predict `overpass` events per AOI from the newest OMM per
    satellite and upsert them. Ids embed the rise second, which shifts when the elements are
    refreshed, so a new pass reuses the id of an existing one for the same satellite/AOI within
    10 minutes (no duplicates). Recomputed at most every `OVERPASS_REFRESH_H`."""
    sats = ccfg.get("satellites") or {}
    if not sats or not aois:
        return 0
    since = now - timedelta(hours=OVERPASS_REFRESH_H)
    if any(ts >= since for ts, _ in store.metric_history("celestrak", OVERPASS_METRIC, since)):
        return 0
    horizon = float(ccfg.get("pass_horizon_h", 48))
    min_elev = float(ccfg.get("min_elev_deg", 20))
    latest: dict[int, GeoEvent] = {}
    for e in events_window(store, ["orbit_elements"], timedelta(days=4), now):
        n = e.attrs.get("norad_id")
        if isinstance(n, int) and n in sats and (n not in latest or e.ts > latest[n].ts):
            latest[n] = e
    if not latest:
        return 0
    # whole-hour grid, 20 min back: no LEO pass above min_elev lasts that long, so none is cut
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(minutes=20)
    existing: dict[tuple[int, str], list[GeoEvent]] = {}
    for e in events_window(store, ["overpass"], timedelta(days=3), now + timedelta(hours=horizon)):
        existing.setdefault((int(e.attrs.get("norad_id", 0)), str(e.attrs.get("aoi"))), []).append(
            e
        )
    rows: list[GeoEvent] = []
    for norad, ev in sorted(latest.items()):
        omm = ev.attrs.get("omm")
        if not isinstance(omm, dict):
            continue
        for aoi in aois:
            try:
                found = orbits.passes(
                    omm,
                    (aoi.lat, aoi.lon),
                    start,
                    horizon,
                    min_elev,
                    aoi=aoi.name,
                    sensor=str(sats[norad].get("sensor", "")),
                )
            except (ValueError, KeyError, TypeError) as e:  # bad elements: skip that satellite
                log.warning("overpass_failed", norad=norad, error=type(e).__name__)
                break
            old = existing.get((norad, aoi.name), [])
            for p in found:
                near = [o for o in old if abs((o.ts - p.ts).total_seconds()) < 600]
                rows.append(p.model_copy(update={"id": near[0].id}) if near else p)
    n = store.insert_events(rows) if rows else 0
    store.record_metric("celestrak", OVERPASS_METRIC, now, float(len(rows)))
    return n


def _assign_tick_persistence(
    store: Store, aoi: str, evs: Sequence[Evidence], step: timedelta
) -> list[Evidence]:
    """Persistence for event-driven kinds = consecutive detection windows (`step` apart) in which
    the subject triggered. Repeat ticks inside one window keep the count (idempotent)."""
    out: list[Evidence] = []
    for e in evs:
        key = persistence_key(aoi, e)
        end = e.window_end
        lo, hi = end - step * 1.5, end - step * 0.5
        prev = [v for ts, v in store.metric_history(key, PERSIST_TICKS, lo) if lo <= ts <= hi]
        ticks = 1 + int(max(prev, default=0.0))
        store.record_metric(key, PERSIST_TICKS, end, float(ticks))
        feats = {**e.features, "persist_ticks": float(ticks)}
        out.append(e.model_copy(update={"persistence": ticks, "features": feats}))
    return out


def _p3_evidence(
    store: Store,
    aoi: AOI,
    p3: P3Windows,
    detectors: Mapping[str, dict[str, Any]],
    now: datetime,
    res: TickResult,
) -> list[Evidence]:
    tcfg = dict(detectors.get("thermal", {}))
    ocfg = dict(detectors.get("outage", {}))
    ncfg = dict(detectors.get("night_lights", {}))
    thm = thermal.detect(p3.thermal, p3.thermal, tcfg, now, aoi=aoi)
    out = outage.detect(p3.outage, ocfg, now, aoi=aoi)
    nl = night_lights.detect(p3.night, ncfg, now, aoi=aoi)
    t_step = float(tcfg.get("step_min", thermal.DEFAULTS["step_min"]))
    o_step = float(ocfg.get("step_min", outage.DEFAULTS["step_min"]))
    evs = _assign_tick_persistence(store, aoi.name, thm, timedelta(minutes=t_step))
    evs += _assign_tick_persistence(store, aoi.name, out, timedelta(minutes=o_step))
    # night lights: the detector's N4 already counts consecutive dropped nights
    evs += [
        e.model_copy(update={"persistence": max(1, int(e.features.get("n_dropped_nights", 1)))})
        for e in nl
    ]

    # metrics per r4 cell (history for later tuning)
    _, w_end = align_window(
        now, float(tcfg.get("window_h", thermal.DEFAULTS["window_h"])) * 60, t_step
    )
    w_start = w_end - timedelta(hours=float(tcfg.get("window_h", thermal.DEFAULTS["window_h"])))
    counts: dict[str, int] = {}
    for e in p3.thermal:
        if (
            e.lat is not None
            and e.lon is not None
            and w_start <= e.ts < w_end
            and aoi.contains(e.lat, e.lon)
        ):
            cell = h3_cell(e.lat, e.lon, 4)
            counts[cell] = counts.get(cell, 0) + 1
    for cell, n in sorted(counts.items()):
        store.record_metric(cell, FIRMS_METRIC, w_end, float(n))
        res.metrics += 1
    latest: dict[str, GeoEvent] = {}
    for e in p3.night:
        cell = str(e.attrs.get("h3_r4") or e.h3_r4 or "")
        if not cell or e.value is None:
            continue
        clat, clon = h3_center(cell)
        if aoi.contains(clat, clon) and (cell not in latest or e.ts > latest[cell].ts):
            latest[cell] = e
    for cell, e in sorted(latest.items()):
        store.record_metric(cell, NIGHT_METRIC, e.ts, float(e.value or 0.0))
        res.metrics += 1
    return evs


def run_aoi(
    store: Store,
    aoi: AOI,
    obs: Sequence[AircraftObs],
    detectors: Mapping[str, dict[str, Any]],
    now: datetime,
    playbook: Mapping[str, Any] | None,
    grades: Mapping[str, str],
    vessels: Sequence[VesselObs] = (),
    events: Sequence[GeoEvent] = (),
    vessels_all: Sequence[VesselObs] | None = None,
    gaps: Sequence[dict[str, Any]] | None = None,
    p3: P3Windows | None = None,
) -> TickResult:
    """One AOI step. `vessels`: AIS window for the AOI; `vessels_all`: the unfiltered AIS window
    for dark-gap detection (ships leaving the AOI keep reporting outside it; a gap belongs to the
    AOI of the ship's last fix); `events`: active notices + GFW/SAR events
    (context/corroboration only; area notices intersecting the AOI become WARNING_AREA Evidence)."""
    jcfg = dict(detectors.get("jamming", {}))
    scfg = dict(detectors.get("spoofing", {}))
    ocfg = dict(detectors.get("orbit", {}))
    fcfg = dict(detectors.get("fusion", {}))
    aicfg = dict(detectors.get("ais_spoofing", {}))
    agcfg = dict(detectors.get("ais_gap", {}))
    wcfg = dict(detectors.get("warning_area", {}))
    fcfg.setdefault("jam_proximity_km", ocfg.get("jam_proximity_km", 100))
    fcfg.setdefault("critical_min_tankers", ocfg.get("critical_min_tankers", 2))
    days = float(jcfg.get("baseline_days", 14))
    res = TickResult(now=now, n_obs=len(obs))

    j_start, j_end = align_window(now, jcfg.get("window_min", 30), jcfg.get("step_min", 5))
    o_start, o_end = align_window(now, ocfg.get("window_min", 75), ocfg.get("step_min", 5))
    step = timedelta(minutes=float(jcfg.get("step_min", 5)))

    stats = jamming.cell_stats(obs, jcfg, now)
    active = _active_jam_cells(store, aoi.name, sorted(stats), j_end, step)
    jam = jamming.detect(
        obs, jcfg, now, _baseline(store, now, days, j_start), active=active, aoi=aoi.name
    )
    spoof = spoofing.detect(obs, scfg, now, None, aoi=aoi.name)
    orb = orbit.detect(
        obs, ocfg, now, _baseline(store, now, days, o_start), jam_evidence=jam, aoi=aoi.name
    )

    ais_sp = ais_spoofing.detect(vessels, aicfg, now, None, aoi=aoi.name)
    if gaps is None:
        gaps = ais_gap.find_gaps(
            vessels if vessels_all is None else vessels_all,
            agcfg,
            now,
            aoi.contains,
        )
    ais_gp = ais_gap.evidence_from_gaps(gaps, agcfg, now, aoi=aoi.name)
    warn = warning_area.detect(events, wcfg, now, aoi=aoi)

    evs = _assign_persistence(store, aoi.name, [*jam, *spoof, *orb, *ais_sp, *ais_gp, *warn], step)
    if p3 is not None:
        evs += _p3_evidence(store, aoi, p3, detectors, now, res)

    min_ac = int(jcfg.get("min_aircraft", 5))
    for cell, st in stats.items():
        if st.total >= min_ac:
            store.record_metric(cell, jamming.METRIC, j_end, st.ratio)
            res.metrics += 1
    for cell in sorted({e.h3_cell for e in orb if e.h3_cell}):
        store.record_metric(cell, orbit.METRIC, o_end, 1.0)
        res.metrics += 1
    if vessels:  # ais_vessels: distinct MMSIs per r4 cell in the spoofing window
        a_start, a_end = align_window(
            now, ais_spoofing.DEFAULTS["window_min"], step.total_seconds() / 60
        )
        per_cell: dict[str, set[int]] = {}
        for v in vessel_in_window(vessels, a_start, a_end):
            per_cell.setdefault(vessel_cell_of(v, 4), set()).add(v.mmsi)
        for cell, mm in sorted(per_cell.items()):
            store.record_metric(cell, AIS_METRIC, a_end, float(len(mm)))
            res.metrics += 1

    open_alerts = store.list_alerts(status=list(ACTIVE), aoi=aoi.name, limit=10_000)
    # recently dismissed alerts own their continuing episode (no re-alert chattering)
    clear_gap = timedelta(minutes=float(fcfg.get("expire_after_min", 60)))
    open_alerts += store.list_alerts(
        since=now - 2 * clear_gap, status=[AlertStatus.DISMISSED], aoi=aoi.name, limit=10_000
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
        notices=events,
    )
    for e in evs:
        store.upsert_evidence(e)
    written: list[Alert] = []
    for a in alerts:
        # ADR-003: re-read immediately before upsert; never overwrite analyst-owned fields
        merged = apply_evidence_update(
            store.get_alert(a.id),
            a,
            expire_before=now - timedelta(minutes=float(fcfg.get("expire_after_min", 60))),
        )
        store.upsert_alert(merged)
        written.append(merged)
    alerts = written
    res.evidence, res.alerts = evs, alerts
    return res


def _assign_gaps(
    aois: Sequence[AOI],
    dets: Mapping[str, Mapping[str, Any]],
    ves_all: Sequence[VesselObs],
    now: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Find AIS gaps once per distinct `ais_gap` config over the unfiltered window and give each
    to exactly one AOI: the nearest centre whose radius contains the ship's last fix."""
    out: dict[str, list[dict[str, Any]]] = {a.name: [] for a in aois}
    groups: dict[str, list[AOI]] = {}
    for a in aois:
        groups.setdefault(json.dumps(dets[a.name].get("ais_gap", {}), sort_keys=True), []).append(a)
    for key, members in groups.items():
        for g in ais_gap.find_gaps(ves_all, json.loads(key), now):
            la, lo = g["last"].lat, g["last"].lon
            inside = [
                (haversine_km(la, lo, a.lat, a.lon), a.name) for a in members if a.contains(la, lo)
            ]
            if inside:
                out[min(inside)[1]].append(g)
    return out


def tick(
    store: Store,
    settings: Settings,
    now: datetime,
    *,
    playbook: Mapping[str, Any] | None = None,
    source_grades: Mapping[str, str] | None = None,
    sources_cfg: Mapping[str, Any] | None = None,
) -> TickResult:
    """One detection step at `now` for every configured AOI."""
    if playbook is None or source_grades is None:
        from heimdall import config as _config

        playbook = _config.playbook() if playbook is None else playbook
        source_grades = (
            grades_from_sources(_config.sources()) if source_grades is None else source_grades
        )
    dets = {a.name: settings.detectors_for(a) for a in settings.aois}  # per-AOI overrides
    lookback = max(
        float(det.get(k, {}).get("window_min", d))
        + float(det.get("jamming", {}).get("step_min", 5))
        for det in [settings.detectors, *dets.values()]
        for k, d in (("jamming", 30), ("spoofing", 30), ("orbit", 75))
    )
    # 024g: worldwide display rows (no NIC/NACp) never feed AOI detectors
    obs_all = [
        o for o in store.aircraft_window(now - timedelta(minutes=lookback), now) if not o.is_global
    ]
    v_lookback = max(
        float(d.get("ais_gap", {}).get("window_h", 24)) * 60 + 5
        for d in [settings.detectors, *dets.values()]
    )
    ves_all = [
        v for v in store.vessels_window(now - timedelta(minutes=v_lookback), now) if not v.is_global
    ]
    notice_days = max(
        float(d.get("warning_area", {}).get("notice_lookback_days", 30))
        for d in [settings.detectors, *dets.values()]
    )
    events_all = [
        e
        for e in store.events_window(now - timedelta(days=notice_days), now)
        if e.kind not in warning_area.NOTICE_KINDS or warning_area.is_active(e, now)
    ]
    gaps_by_aoi = _assign_gaps(settings.aois, dets, ves_all, now)
    if sources_cfg is None:
        from heimdall import config as _config

        sources_cfg = _config.sources().get("sources", {})
    update_overpasses(store, settings.aois, sources_cfg.get("celestrak", {}), now)
    p3 = _p3_windows(store, dets, now)
    total = TickResult(now=now, n_obs=len(obs_all))
    for aoi in settings.aois:
        obs = [o for o in obs_all if aoi.contains(o.lat, o.lon)]
        ves = [v for v in ves_all if aoi.contains(v.lat, v.lon)]
        r = run_aoi(
            store,
            aoi,
            obs,
            dets[aoi.name],
            now,
            playbook,
            source_grades,
            ves,
            events_all,
            vessels_all=ves_all,
            gaps=gaps_by_aoi[aoi.name],
            p3=p3,
        )
        total.evidence += r.evidence
        total.alerts += r.alerts
        total.metrics += r.metrics
    return total
