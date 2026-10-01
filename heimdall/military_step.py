"""Pipeline step for military air activity (task 039): tracks -> theatres -> Evidence -> alerts.

Runs once per tick after the AOI steps. State that outlives the 1 h hot-store retention of
worldwide rows lives in `MilTracks` (module-level, one per store) and is saved to
`detectors.military.state_path` every `save_every_min` (no path = memory only, e.g. tests).
Theatres = config presets (`detectors.military.theatres`) + every analyst area.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from heimdall.detect import military
from heimdall.detect.warning_area import is_active
from heimdall.fuse.corroborate import CORROBORATING_CATEGORIES
from heimdall.geo import KM_PER_NM, h3_cell, haversine_km
from heimdall.ingest import plane_alert
from heimdall.logging import get_logger
from heimdall.milroles import MilIdent, classify
from heimdall.miltrack import MilTracks
from heimdall.ports import Store
from heimdall.schemas import AlertKind, Evidence, GeoEvent

log = get_logger("heimdall.military")

MIL_KINDS = (
    AlertKind.MIL_DEPLOYMENT,
    AlertKind.MIL_SURGE,
    AlertKind.MIL_HIGH_INTEREST,
    AlertKind.MIL_BUILDUP,
)
STEP_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "state_path": None,
    "save_every_min": 10,
    "baseline_days": 14,
    "first_read_min": 75,  # on a cold start, read this much back from the hot store
    "theatres": [],
}


@dataclass
class MilState:
    store: Store
    tracks: MilTracks
    path: Path | None
    saved_at: datetime | None = None
    idents: dict[tuple[str, str | None], MilIdent] = field(default_factory=dict)


_STATE: list[MilState] = []


def state_for(store: Store, c: Mapping[str, Any]) -> MilState:
    if _STATE and _STATE[0].store is store:
        return _STATE[0]
    raw = os.environ.get("HEIMDALL_MIL_STATE", c.get("state_path") or "")  # tests: tmp or ""
    path = Path(raw) if raw else None
    tracks = MilTracks()
    if path is not None and path.exists():
        try:
            tracks = MilTracks.from_json(path.read_text())
        except (OSError, ValueError, TypeError) as e:
            log.warning("mil_tracks_unreadable", error=type(e).__name__)
    _STATE[:] = [MilState(store, tracks, path)]
    return _STATE[0]


def theatres(cfg: Mapping[str, Any], aois: Sequence[Any]) -> list[military.Theatre]:
    out = [
        military.Theatre(
            str(t["name"]),
            float(t["lat"]),
            float(t["lon"]),
            float(t["radius_km"]),
            tuple(str(r) for r in t.get("rules") or military.ALL_RULES),
        )
        for t in cfg.get("theatres") or []
        if isinstance(t, Mapping)
    ]
    names = {t.name for t in out}
    out += [
        military.Theatre(a.name, a.lat, a.lon, a.radius_nm * KM_PER_NM)
        for a in aois
        if a.name not in names
    ]
    return out


def identities(st: MilState) -> dict[str, MilIdent]:
    db = plane_alert.index()
    out: dict[str, MilIdent] = {}
    for h, tr in st.tracks.tracks.items():
        key = (h, tr.type_code)
        ident = st.idents.get(key)
        if ident is None or (not ident.known and h in db):
            ident = st.idents[key] = classify(h, tr.type_code, db.get(h))
        out[h] = ident
    return out


def _near(th: military.Theatre, lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None
        and lon is not None
        and haversine_km(lat, lon, th.lat, th.lon) <= (th.radius_km)
    )


def military_step(
    store: Store,
    cfg: Mapping[str, Any],
    aois: Sequence[Any],
    notices: Sequence[GeoEvent],
    now: datetime,
    fuse_and_store: Callable[[str, list[Evidence]], object],
    persist: Callable[[Store, str, list[Evidence], timedelta], list[Evidence]],
) -> list[Evidence]:
    """One tick: absorb new military rows, run the theatre detectors, fuse per theatre."""
    c = {**STEP_DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}
    if not c["enabled"]:
        return []
    st = state_for(store, c)
    lo = (
        datetime.fromtimestamp(st.tracks.last_ts - 60, tz=now.tzinfo)
        if st.tracks.last_ts
        else now - timedelta(minutes=float(c["first_read_min"]))
    )
    lo = max(lo, now - timedelta(minutes=float(c["first_read_min"])))
    st.tracks.add(store.aircraft_window(lo, now + timedelta(seconds=1), mil_only=True))
    st.tracks.prune(now)
    if st.path is not None and (
        st.saved_at is None or now - st.saved_at >= timedelta(minutes=float(c["save_every_min"]))
    ):
        try:
            st.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = st.path.with_suffix(".tmp")
            tmp.write_text(st.tracks.to_json())
            tmp.replace(st.path)
            st.saved_at = now
        except OSError as e:
            log.warning("mil_tracks_save_failed", error=type(e).__name__)
    if not st.tracks.tracks:
        return []

    idents = identities(st)
    snap = military.Snapshot.of(st.tracks)
    step = timedelta(minutes=float(c.get("step_min", military.DEFAULTS["step_min"])))
    since_base = now - timedelta(days=float(c["baseline_days"]))
    window = timedelta(hours=float(c.get("buildup_window_h", 72)))
    jams = [
        a
        for a in store.list_alerts(since=now - window, limit=10_000)
        if a.kind == AlertKind.GNSS_JAMMING
    ]
    mil_notices = [
        n
        for n in notices
        if is_active(n, now)
        and str(n.attrs.get("category") or "") in (*CORROBORATING_CATEGORIES, "airspace")
    ]
    out: list[Evidence] = []
    for th in theatres(c, aois):

        def base(role: str, th: military.Theatre = th) -> list[float]:
            hist = store.metric_history(f"{th.name}|{role}", military.METRIC, since_base)
            return [v for _, v in hist]

        evs, counts = military.detect_movements(snap, th, idents, now, c, base)
        hour = now.replace(minute=0, second=0, microsecond=0)
        for role in military.DEFAULTS["surge_min"]:
            store.record_metric(f"{th.name}|{role}", military.METRIC, hour, counts.get(role, 0.0))
        evs = persist(store, th.name, evs, step)
        prior = [
            e
            for k in (AlertKind.MIL_DEPLOYMENT, AlertKind.MIL_SURGE, AlertKind.MIL_HIGH_INTEREST)
            for e in store.evidence_history(h3_cell(th.lat, th.lon, 3), k, now - window)
            if e.aoi == th.name
        ]
        bu = military.detect_buildup(
            [*prior, *evs],
            snap,
            th,
            idents,
            now,
            c,
            n_notices=sum(1 for n in mil_notices if _near(th, n.lat, n.lon)),
            n_jamming=sum(1 for a in jams if _near(th, a.lat, a.lon)),
        )
        evs += persist(store, th.name, bu, step)
        fuse_and_store(th.name, evs)  # also expires this theatre's stale military alerts
        out += evs
    return out
