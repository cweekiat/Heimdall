"""Pipeline tick: Store window -> detectors per AOI -> persistence -> metrics -> fusion -> Store.

Orchestration only; all maths lives in heimdall/detect and heimdall/fuse. Talks to infrastructure
exclusively through the `Store` port.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from heimdall import orbits
from heimdall.config import AOI, Settings
from heimdall.detect import (
    ais_gap,
    ais_spoofing,
    cygnss,
    hf,
    jamming,
    orbit,
    spectrum_state,
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
from heimdall.geo import h3_cell, haversine_km
from heimdall.logging import get_logger
from heimdall.military_step import MIL_KINDS, military_step
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
    if ev.kind == AlertKind.SPECTRUM_ANOMALY and ev.bands:
        return f"{aoi}|{ev.kind}|band:{ev.bands[0]}"
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
OVERPASS_METRIC = "overpass_refresh"  # last time overpasses were (re)computed
OVERPASS_REFRESH_H = 6.0  # predictions move by seconds per day; recompute a few times a day
log = get_logger("heimdall.pipeline")


@dataclass
class P3Windows:
    """Event windows for the P3 detectors (ADR-007), shared by all AOIs of one tick."""

    thermal: Sequence[GeoEvent] = ()  # detector window + baseline history in one list
    context: Sequence[GeoEvent] = ()  # confounders/observability for fusion (ADR-007, task 022b)


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
    return P3Windows(
        thermal=events_window(store, ["thermal"], timedelta(days=t_days), now),
        # weather/Kp for confounders; footprints and overpasses (+/- horizon) for observability
        context=store.events_window(
            now - timedelta(days=2),
            now + timedelta(hours=48),
            ["weather", "space_weather", "imagery_footprint", "overpass"],
        ),
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
    thm = thermal.detect(p3.thermal, p3.thermal, tcfg, now, aoi=aoi)
    t_step = float(tcfg.get("step_min", thermal.DEFAULTS["step_min"]))
    evs = _assign_tick_persistence(store, aoi.name, thm, timedelta(minutes=t_step))

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
    return evs


def spectrum_windows(store: Store, now: datetime) -> list[GeoEvent]:
    """Spectrum events each band indicator needs, one read per kind with its own look-back."""
    out: list[GeoEvent] = []
    for kind, span in spectrum_state.EVENT_WINDOWS.items():
        out += store.events_window(now - span, now + timedelta(minutes=5), [kind])
    return out


def spectrum_step(
    store: Store,
    aoi: AOI,
    obs: Sequence[AircraftObs],
    vessels: Sequence[VesselObs],
    events: Sequence[GeoEvent],
    detectors: Mapping[str, dict[str, Any]],
    now: datetime,
    res: TickResult,
) -> list[Evidence]:
    """033/034: band indicators -> metric history -> state; anomalous bands -> Evidence."""
    cfg = dict(detectors.get("spectrum_state", {}))
    jcfg = detectors.get("jamming", {})
    days = float(cfg.get("baseline_days", spectrum_state.DEFAULTS["baseline_days"]))
    step = float(jcfg.get("step_min", 5))
    window = align_window(now, 60, step)
    inds = spectrum_state.compute_indicators(
        aoi,
        obs,
        vessels,
        events,
        now,
        int(jcfg.get("nic_bad_below", 7)),
        int(jcfg.get("nacp_bad_below", 8)),
    )
    out: list[Evidence] = []
    for band, ind in inds.items():
        key = f"{aoi.name}|{band.value}"
        hist = store.metric_history(key, spectrum_state.METRIC, now - timedelta(days=days))
        past = [(ts, v) for ts, v in hist if ts < window[0]]
        st = spectrum_state.assess(
            aoi.name,
            ind,
            spectrum_state.history_for(band, past, now),
            cfg,
            spectrum_state.natural_explanations(band, events, now),
        )
        if ind.value is not None and ind.n >= int(spectrum_state.INDICATORS[band]["min_n"]):
            store.record_metric(key, spectrum_state.METRIC, window[1], float(ind.value))
            store.record_metric(key, spectrum_state.N_METRIC, window[1], float(ind.n))
            res.metrics += 2
        ev = spectrum_state.evidence(st, aoi, now, window, cfg)
        if ev is not None:
            out.append(ev)
    return _assign_tick_persistence(store, aoi.name, out, timedelta(minutes=step))


# (store, hour, cell-days, space weather); the store reference keeps identity checks honest
_CYG_CACHE: list[tuple[Store, datetime, list[GeoEvent], list[GeoEvent]]] = []


def cygnss_windows(
    store: Store, dets: Mapping[str, Mapping[str, Any]], now: datetime
) -> tuple[list[GeoEvent], list[GeoEvent]]:
    """CYGNSS cell-days + space weather for the CYGNSS detector. The data are daily (latency
    2-3 days, ~10k cells/day in the bbox), so one read per hour is shared by every tick."""
    days = (
        max(
            float(d.get("cygnss", {}).get("max_age_days", cygnss.DEFAULTS["max_age_days"]))
            for d in dets.values()
        )
        if dets
        else float(cygnss.DEFAULTS["max_age_days"])
    )
    hour = now.replace(minute=0, second=0, microsecond=0)
    if _CYG_CACHE and _CYG_CACHE[0][0] is store and _CYG_CACHE[0][1] == hour:
        return _CYG_CACHE[0][2], _CYG_CACHE[0][3]
    lo, hi = now - timedelta(days=days + 1), now + timedelta(seconds=1)
    evs = store.events_window(lo, hi, [cygnss.KIND])
    sw = store.events_window(lo, hi, ["space_weather"])
    _CYG_CACHE[:] = [(store, hour, evs, sw)]
    return evs, sw


class HfCache:
    """P6: spot bins for the HF detector. Past days' bins are stable, so each 10-min bin of a
    baseline day is read once and kept; the current day's span is re-read every run (late
    uploads complete recent bins). One `hf.prepare` per newest-window end is shared by all AOIs."""

    def __init__(self) -> None:
        self.store: Store | None = None
        self.hist: dict[datetime, list[hf.SpotBin]] = {}
        self.t_end: datetime | None = None
        self.state: hf.HfState | None = None

    def bins_for(self, store: Store, t_end: datetime, cfg: Mapping[str, Any]) -> list[hf.SpotBin]:
        if store is not self.store:
            self.store, self.hist, self.t_end, self.state = store, {}, None, None
        sp, ten = hf.span(cfg), timedelta(minutes=10)
        days = int(cfg.get("baseline_days", hf.DEFAULTS["baseline_days"]))
        wanted: set[datetime] = set()
        for d in range(1, days + 1):
            t = t_end - timedelta(days=d) - sp
            while t < t_end - timedelta(days=d):
                wanted.add(t)
                t += ten
        self.hist = {t: v for t, v in self.hist.items() if t in wanted}  # slid out: drop
        need = [t for t in wanted if t not in self.hist]
        if need:
            got: dict[datetime, list[hf.SpotBin]] = {t: [] for t in need}
            # contiguous runs -> one read each (first run: one per baseline day)
            runs: list[list[datetime]] = []
            for t in sorted(need):
                if runs and t - runs[-1][-1] == ten:
                    runs[-1].append(t)
                else:
                    runs.append([t])
            for run in runs:
                evs = store.events_window(run[0], run[-1] + ten, [hf.KIND])
                for b in hf.bins_from_events(evs):
                    if b.t in got:
                        got[b.t].append(b)
            self.hist.update(got)
        cur = hf.bins_from_events(store.events_window(t_end - sp, t_end, [hf.KIND]))
        return [b for bs in self.hist.values() for b in bs] + cur

    def state_for(self, store: Store, cfg: Mapping[str, Any], now: datetime) -> hf.HfState | None:
        c = {**hf.DEFAULTS, **cfg}
        if not c.get("enabled", True):
            return None
        t_end = hf.newest_window_end(now, c)
        if store is self.store and self.t_end == t_end and self.state is not None:
            return self.state
        bins = self.bins_for(store, t_end, c)
        ctx = store.events_window(
            t_end - hf.span(c) - timedelta(hours=7), t_end, list(hf.CONTEXT_KINDS)
        )
        self.state, self.t_end = hf.prepare(bins, ctx, t_end, c, created=now), t_end
        return self.state


_HF = HfCache()


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
    spectrum_events: Sequence[GeoEvent] | None = None,
    cygnss_events: tuple[Sequence[GeoEvent], Sequence[GeoEvent]] | None = None,
    hf_state: hf.HfState | None = None,
) -> TickResult:
    """One AOI step. `vessels`: AIS window for the AOI; `vessels_all`: the unfiltered AIS window
    for dark-gap detection (ships leaving the AOI keep reporting outside it; a gap belongs to the
    AOI of the ship's last fix); `events`: active notices + SAR events
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
    if spectrum_events is not None:
        evs += spectrum_step(store, aoi, obs, vessels, spectrum_events, detectors, now, res)
    if cygnss_events is not None:  # persistence = flagged days, set by the detector itself
        cy_ev, cy_sw = cygnss_events
        evs += cygnss.detect(
            cy_ev, dict(detectors.get("cygnss", {})), now, aoi=aoi, space_weather=cy_sw
        )
    if hf_state is not None:  # P6: persistence = consecutive flagged windows, set by the detector
        evs += hf.detect(hf_state, aoi, dict(detectors.get("hf", {})), now)

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

    alerts = fuse_and_store(
        store,
        aoi.name,
        evs,
        fcfg,
        playbook,
        grades,
        now,
        notices=events,
        context_events=p3.context if p3 is not None else None,
        control=aoi.role == "control",
        own=lambda k: k not in MIL_KINDS,  # task 039: theatre alerts are fused by military_step
    )
    res.evidence, res.alerts = evs, alerts
    return res


def expire_unmonitored(
    store: Store,
    monitored: set[str],
    mcfg: Mapping[str, Any],
    fcfg: Mapping[str, Any],
    now: datetime,
) -> int:
    """041: NEW alerts of an area that is no longer monitored (deleted or switched off, or a
    config preset in live mode) never see another tick, so they would stay NEW for ever. Expire
    them once stale, like any alert without fresh evidence. Analyst statuses are never touched."""
    from heimdall.military_step import theatres

    keep = monitored | {t.name for t in theatres(mcfg, [])}
    horizon = now - timedelta(minutes=float(fcfg.get("expire_after_min", 60)))
    n = 0
    for a in store.list_alerts(status=[AlertStatus.NEW], limit=10_000):
        if a.aoi is not None and a.aoi not in keep and a.last_seen < horizon:
            store.upsert_alert(a.model_copy(update={"status": AlertStatus.EXPIRED}))
            n += 1
    if n:
        log.info("expired_unmonitored_alerts", n=n)
    return n


def fuse_and_store(
    store: Store,
    aoi: str,
    evs: Sequence[Evidence],
    fcfg: Mapping[str, Any],
    playbook: Mapping[str, Any] | None,
    grades: Mapping[str, str],
    now: datetime,
    *,
    notices: Sequence[GeoEvent] = (),
    context_events: Sequence[GeoEvent] | None = None,
    control: bool = False,
    own: Callable[[AlertKind], bool] = lambda k: True,
) -> list[Alert]:
    """Fuse Evidence with this area's open alerts of the kinds this caller owns; store both."""
    open_alerts = [
        a for a in store.list_alerts(status=list(ACTIVE), aoi=aoi, limit=10_000) if own(a.kind)
    ]
    # recently dismissed alerts own their continuing episode (no re-alert chattering)
    clear_gap = timedelta(minutes=float(fcfg.get("expire_after_min", 60)))
    open_alerts += [
        a
        for a in store.list_alerts(
            since=now - 2 * clear_gap, status=[AlertStatus.DISMISSED], aoi=aoi, limit=10_000
        )
        if own(a.kind)
    ]
    alerts = fuse(
        evs,
        open_alerts,
        now,
        fcfg,
        playbook,
        grades,
        aoi=aoi,
        control=control,
        notices=notices,
        context_events=context_events,
    )
    for e in evs:
        store.upsert_evidence(e)
    written: list[Alert] = []
    for a in alerts:
        # ADR-003: re-read immediately before upsert; never overwrite analyst-owned fields
        merged = apply_evidence_update(store.get_alert(a.id), a, expire_before=now - clear_gap)
        store.upsert_alert(merged)
        written.append(merged)
    return written


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
    # filtered in the store: loading them and dropping them here cost ~0.5 M objects per tick
    obs_all = store.aircraft_window(now - timedelta(minutes=lookback), now, local_only=True)
    v_lookback = max(
        float(d.get("ais_gap", {}).get("window_h", 24)) * 60 + 5
        for d in [settings.detectors, *dets.values()]
    )
    ves_all = store.vessels_window(now - timedelta(minutes=v_lookback), now, local_only=True)
    notice_days = max(
        float(d.get("warning_area", {}).get("notice_lookback_days", 30))
        for d in [settings.detectors, *dets.values()]
    )
    events_all = [
        e
        # notices only: spectrum sources add ~2k events/h; a kind-less 30-day read loads them all
        for e in store.events_window(
            now - timedelta(days=notice_days), now, list(warning_area.NOTICE_KINDS)
        )
        if warning_area.is_active(e, now)
    ]
    gaps_by_aoi = _assign_gaps(settings.aois, dets, ves_all, now)
    if sources_cfg is None:
        from heimdall import config as _config

        sources_cfg = _config.sources().get("sources", {})
    update_overpasses(store, settings.aois, sources_cfg.get("celestrak", {}), now)
    p3 = _p3_windows(store, dets, now)
    spec_events = spectrum_windows(store, now)
    cyg = cygnss_windows(store, dets, now)
    hf_st = _HF.state_for(store, dict(settings.detectors.get("hf", {})), now)
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
            spectrum_events=spec_events,
            cygnss_events=cyg,
            hf_state=hf_st,
        )
        total.evidence += r.evidence
        total.alerts += r.alerts
        total.metrics += r.metrics
    mcfg = settings.detectors.get("military", {})
    fcfg = dict(settings.detectors.get("fusion", {}))
    expire_unmonitored(store, {a.name for a in settings.aois}, mcfg, fcfg, now)
    total.evidence += military_step(
        store,
        mcfg,
        settings.aois,
        events_all,
        now,
        lambda name, evs: fuse_and_store(
            store,
            name,
            evs,
            fcfg,
            playbook,
            source_grades,
            now,
            notices=events_all,
            own=lambda k: k in MIL_KINDS,
        ),
        _assign_tick_persistence,
    )
    return total
