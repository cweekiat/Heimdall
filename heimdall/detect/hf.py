"""HF path anomaly at ionospheric path midpoints (P6, task 045, ADR-008). Experimental. Pure.

Input: `hf_spots` GeoEvents (WSPR Live, PSK Reporter): spots aggregated per 10-min bin, amateur
band and 2-degree grid cell of the transmitter-receiver great-circle midpoint, which is roughly
where the signal reflected off the ionosphere. Space weather (`space_weather` Kp/R scales,
`solar_xray` GOES flux) is used to rule out natural causes.

Cells are regrouped to `cell_deg` (4 degrees, ~440 km): at 2 degrees the median cell hears one
spot per 10-min bin from one receiver (WSPR, Indo-Pacific box, Sep 2026), too few to judge.
For each (source, band, midpoint cell) and a 60-min window, the value is compared with the same
clock window on each of the previous `baseline_days` days (HF follows the sun, so only the same
time of day is comparable). A day counts as a sample only if that source reported anything in the
whole box in that window (a missing feed is not "zero spots"):
* H1 spot-count drop: usual median >= min_expected spots from >= min_rx receivers, now
  <= drop_ratio x usual and robust z <= -z_min.
* H2 SNR drop: >= snr_min_spots now and on >= min_baseline_days days; median SNR fell by
  >= snr_drop_db and robust z <= -z_min.
Natural explanations, checked per window; an explained window is NOT flagged (counted only):
* solar flare: GOES 0.1-0.8 nm flux >= flare_flux (M1) from flare_lead_min before the window to
  its end, with the midpoint sunlit (sun elevation >= sunlit_elev_deg): shortwave fade-out;
  SWPC R >= 1 is used the same way when no flux sample covers the window;
* geomagnetic storm: Kp >= kp_explain in the 6 h before the window end;
* feed / network drop: the source's spot total in the whole box < network_ok_ratio x usual.
Persistence = consecutive flagged windows `step_min` apart (fusion requires 2). Confidence is
capped (single experimental band, <= medium) and the alert never names an emitter type: a drop
can also be receivers switched off, which aggregated spots cannot tell apart (stated in gaps).
All thresholds are priors from the backfill replays in `docs/reports/2026-10-03-hf.md`.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from heimdall.detect.common import MAD_SCALE, evidence_id
from heimdall.geo import KM_PER_NM, h3_cell, haversine_km
from heimdall.schemas import (
    AlertKind,
    Band,
    Coverage,
    Evidence,
    GeoEvent,
    RecordRef,
    RuleResult,
)

NAME = "hf_path"
VERSION = "hf-path-0.1.0"
KIND = "hf_spots"
CONTEXT_KINDS = ("space_weather", "solar_xray")
H3_RES = 3

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "cell_deg": 4.0,  # 2-degree source cells regrouped: most 2-degree cells hear ~1 spot per bin
    "window_min": 60,
    "step_min": 30,  # persistence spacing between evaluated windows
    "persist_windows": 3,  # windows evaluated per run (current + 2 earlier)
    "settle_min": 20,  # upload lag: the newest window ends this long before now
    "baseline_days": 14,
    "min_baseline_days": 5,
    "min_expected": 30,  # usual spots per window before a drop means anything
    "min_rx": 3,  # usual distinct receivers (one station going offline is not an anomaly)
    "drop_ratio": 0.25,
    "z_min": 3.0,
    "mad_floor_frac": 0.15,  # count scale floor as a share of the usual median
    "snr_min_spots": 10,
    "snr_drop_db": 8.0,
    "snr_mad_floor_db": 2.0,
    "flare_flux": 1e-5,  # M1
    "flare_lead_min": 30,
    "sunlit_elev_deg": -6.0,  # D layer is lit somewhat beyond the ground terminator
    "kp_explain": 5.0,
    "network_ok_ratio": 0.6,
    "reach_km": 500,  # midpoint cells this far beyond an area's edge count for it
    "confidence_cap": 0.5,
}

Key = tuple[str, int, float, float]  # source, band (MHz label), midpoint grid lat, lon


class AoiLike(Protocol):
    name: str
    lat: float
    lon: float
    radius_nm: float


@dataclass(frozen=True)
class SpotBin:
    id: str
    source: str
    band: int
    glat: float
    glon: float
    t: datetime
    n: float
    n_rx: float
    snr: float | None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def bins_from_events(events: Iterable[GeoEvent]) -> list[SpotBin]:
    out = []
    for e in events:
        if e.kind != KIND or e.lat is None or e.lon is None:
            continue
        band, n = e.attrs.get("band_mhz"), _num(e.attrs.get("n"))
        if not isinstance(band, int) or not n:
            continue
        out.append(
            SpotBin(
                e.id,
                e.source,
                band,
                e.lat,
                e.lon,
                e.ts,
                n,
                _num(e.attrs.get("n_rx")) or 0.0,
                _num(e.attrs.get("snr_med")),
            )
        )
    return out


@dataclass
class Agg:
    n: float = 0.0
    n_rx: float = 0.0  # max per bin: a lower bound on distinct receivers in the window
    snr_w: float = 0.0
    snr_n: float = 0.0
    ids: list[str] = field(default_factory=list)

    @property
    def snr(self) -> float | None:
        return self.snr_w / self.snr_n if self.snr_n else None


def aggregate(bins: Iterable[SpotBin]) -> tuple[dict[Key, Agg], dict[str, float]]:
    """Per-key sums and per-source totals over the given bins."""
    out: dict[Key, Agg] = defaultdict(Agg)
    totals: dict[str, float] = defaultdict(float)
    for b in bins:
        a = out[(b.source, b.band, b.glat, b.glon)]
        a.n += b.n
        a.n_rx = max(a.n_rx, b.n_rx)
        if b.snr is not None:
            a.snr_w += b.snr * b.n
            a.snr_n += b.n
        a.ids.append(b.id)
        totals[b.source] += b.n
    return dict(out), dict(totals)


def sun_elevation_deg(lat: float, lon: float, t: datetime) -> float:
    """Approximate solar elevation (about 1 degree accuracy; enough for day / night)."""
    doy = t.timetuple().tm_yday
    g = 2 * math.pi / 365.0 * (doy - 1 + (t.hour - 12) / 24)
    decl = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
    )
    eqt = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    minutes = t.hour * 60 + t.minute + t.second / 60
    ha = math.radians((minutes + eqt + 4 * lon) / 4 - 180)
    la = math.radians(lat)
    s = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


@dataclass
class Flag:
    key: Key
    t0: datetime
    t1: datetime
    n: float
    usual_n: float
    z_n: float | None
    snr: float | None
    usual_snr: float | None
    z_snr: float | None
    n_rx: float
    usual_rx: float
    n_days: int
    net_ratio: float
    count_drop: bool
    snr_drop: bool
    explained: list[str]
    ids: list[str]

    @property
    def flagged(self) -> bool:
        return (self.count_drop or self.snr_drop) and not self.explained


@dataclass
class HfState:
    """One run: flags per evaluated window (index 0 = newest) and what was explained away."""

    windows: list[tuple[datetime, datetime]]
    flags: list[dict[Key, Flag]]
    created: datetime

    def explained(self) -> list[Flag]:
        return [f for w in self.flags for f in w.values() if f.explained]


def _robust(xs: Sequence[float]) -> tuple[float, float]:
    med = statistics.median(xs)
    return med, statistics.median(abs(x - med) for x in xs)


def _space_weather(
    context: Sequence[GeoEvent], t0: datetime, t1: datetime, c: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """(flare note or None, storm note or None) for a window; flare is checked per cell later."""
    lead = timedelta(minutes=float(c["flare_lead_min"]))
    xr = [
        e
        for e in context
        if e.kind == "solar_xray" and t0 - lead <= e.ts < t1 and isinstance(e.value, float)
    ]
    flare = None
    if xr:
        top = max(xr, key=lambda e: float(e.value or 0.0))
        if float(top.value or 0.0) >= float(c["flare_flux"]):
            cls = top.attrs.get("flare_class") or f"{top.value:.1e} W/m2"
            flare = f"solar flare {cls} at {top.ts:%H:%M} UTC (shortwave fade-out on sunlit paths)"
    else:
        r = [
            _num(e.attrs.get("r_scale")) or 0.0
            for e in context
            if e.kind == "space_weather" and t0 - timedelta(hours=1) <= e.ts < t1
        ]
        if r and max(r) >= 1:
            flare = f"solar radio blackout R{int(max(r))} (shortwave fade-out on sunlit paths)"
    kp = [
        _num(e.attrs.get("kp")) or 0.0
        for e in context
        if e.kind == "space_weather" and t1 - timedelta(hours=6) <= e.ts < t1
    ]
    storm = None
    if kp and max(kp) >= float(c["kp_explain"]):
        storm = f"geomagnetic storm (Kp {max(kp):g}) disturbs HF propagation"
    return flare, storm


def assess_window(
    by_bin: Mapping[datetime, Sequence[SpotBin]],
    t0: datetime,
    t1: datetime,
    context: Sequence[GeoEvent],
    c: Mapping[str, Any],
) -> dict[Key, Flag]:
    """Flags for every key seen now or usually busy in [t0, t1)."""

    def window(lo: datetime, hi: datetime) -> list[SpotBin]:
        return [b for t, bs in by_bin.items() if lo <= t < hi for b in bs]

    cur, cur_tot = aggregate(window(t0, t1))
    days: list[tuple[dict[Key, Agg], dict[str, float]]] = []
    for d in range(1, int(c["baseline_days"]) + 1):
        shift = timedelta(days=d)
        days.append(aggregate(window(t0 - shift, t1 - shift)))
    flare, storm = _space_weather(context, t0, t1, c)
    mid = t0 + (t1 - t0) / 2
    out: dict[Key, Flag] = {}
    keys = set(cur) | {k for aggs, _ in days for k in aggs}
    for key in keys:
        src = key[0]
        active = [(aggs, tot) for aggs, tot in days if tot.get(src, 0.0) > 0]
        if len(active) < int(c["min_baseline_days"]) or cur_tot.get(src, 0.0) <= 0:
            continue  # learning, or the feed is silent now (nothing to compare)
        ns = [aggs[key].n if key in aggs else 0.0 for aggs, _ in active]
        usual_n, mad_n = _robust(ns)
        if usual_n < float(c["min_expected"]) and key not in cur:
            continue
        now = cur.get(key, Agg())
        rxs = [aggs[key].n_rx if key in aggs else 0.0 for aggs, _ in active]
        usual_rx = statistics.median(rxs)
        sig_n = MAD_SCALE * mad_n + max(3.0, float(c["mad_floor_frac"]) * usual_n)
        z_n = (now.n - usual_n) / sig_n
        count_drop = (
            usual_n >= float(c["min_expected"])
            and usual_rx >= float(c["min_rx"])
            and now.n <= float(c["drop_ratio"]) * usual_n
            and z_n <= -float(c["z_min"])
        )
        snrs = [
            s
            for aggs, _ in active
            if key in aggs
            and aggs[key].n >= float(c["snr_min_spots"])
            and (s := aggs[key].snr) is not None
        ]
        usual_snr = z_snr = None
        snr_drop = False
        if len(snrs) >= int(c["min_baseline_days"]):
            usual_snr, mad_s = _robust(snrs)
            if now.snr is not None and now.n >= float(c["snr_min_spots"]):
                z_snr = (now.snr - usual_snr) / (MAD_SCALE * mad_s + float(c["snr_mad_floor_db"]))
                snr_drop = now.snr - usual_snr <= -float(c["snr_drop_db"]) and z_snr <= -float(
                    c["z_min"]
                )
        if not (count_drop or snr_drop):
            continue
        usual_tot = statistics.median(tot.get(src, 0.0) for _, tot in active)
        net_ratio = cur_tot.get(src, 0.0) / usual_tot if usual_tot else 0.0
        explained = []
        if flare and sun_elevation_deg(key[2], key[3], mid) >= float(c["sunlit_elev_deg"]):
            explained.append(flare)
        if storm:
            explained.append(storm)
        if net_ratio < float(c["network_ok_ratio"]):
            explained.append(
                f"{src} reported {net_ratio:.0%} of its usual spots over the whole region "
                "(feed or region-wide propagation, not a local effect)"
            )
        out[key] = Flag(
            key,
            t0,
            t1,
            now.n,
            usual_n,
            round(z_n, 2),
            now.snr,
            usual_snr,
            None if z_snr is None else round(z_snr, 2),
            now.n_rx,
            usual_rx,
            len(active),
            round(net_ratio, 3),
            count_drop,
            snr_drop,
            explained,
            now.ids[:6],
        )
    return out


def newest_window_end(now: datetime, cfg: Mapping[str, Any] | None = None) -> datetime:
    """End of the newest window: `settle_min` before now, floored to 10 minutes (bin size)."""
    c = {**DEFAULTS, **(cfg or {})}
    t = now - timedelta(minutes=float(c["settle_min"]))
    return t.replace(minute=t.minute - t.minute % 10, second=0, microsecond=0)


def span(cfg: Mapping[str, Any] | None = None) -> timedelta:
    """Time covered by the evaluated windows (the read needed for one day)."""
    c = {**DEFAULTS, **(cfg or {})}
    return timedelta(
        minutes=float(c["window_min"]) + (int(c["persist_windows"]) - 1) * float(c["step_min"])
    )


def prepare(
    bins: Iterable[SpotBin],
    context: Sequence[GeoEvent],
    t_end: datetime,
    cfg: Mapping[str, Any] | None = None,
    created: datetime | None = None,
) -> HfState:
    """Assess the newest window ending `t_end` and the `persist_windows - 1` before it.
    `bins` must cover `span()` before `t_end` on that day and on each baseline day."""
    c = {**DEFAULTS, **(cfg or {})}
    deg = float(c["cell_deg"])
    by_bin: dict[datetime, list[SpotBin]] = defaultdict(list)
    for b in bins:
        if deg > 0:
            b = replace(b, glat=round(b.glat / deg) * deg, glon=round(b.glon / deg) * deg)
        by_bin[b.t].append(b)
    w = timedelta(minutes=float(c["window_min"]))
    step = timedelta(minutes=float(c["step_min"]))
    wins = [(t_end - k * step - w, t_end - k * step) for k in range(int(c["persist_windows"]))]
    flags = [assess_window(by_bin, t0, t1, context, c) for t0, t1 in wins]
    return HfState(wins, flags, created or t_end)


def _near(key: Key, aoi: AoiLike, reach_km: float) -> bool:
    return haversine_km(key[2], key[3], aoi.lat, aoi.lon) <= aoi.radius_nm * KM_PER_NM + reach_km


def detect(
    state: HfState,
    aoi: AoiLike,
    cfg: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> list[Evidence]:
    """SPECTRUM_ANOMALY (band HF) Evidence for flagged, unexplained midpoint cells near `aoi`."""
    c = {**DEFAULTS, **(cfg or {})}
    out: list[Evidence] = []
    for key, f in state.flags[0].items():
        if not f.flagged or not _near(key, aoi, float(c["reach_km"])):
            continue
        persistence = 0
        for w in state.flags:
            if key in w and w[key].flagged:
                persistence += 1
            else:
                break
        out.append(_evidence(f, persistence, aoi, c, now or state.created))
    return out


def _evidence(
    f: Flag, persistence: int, aoi: AoiLike, c: Mapping[str, Any], now: datetime
) -> Evidence:
    src, band, glat, glon = f.key
    cell = h3_cell(glat, glon, H3_RES)
    zmin = float(c["z_min"])
    pct = 100.0 * f.n / f.usual_n if f.usual_n else 0.0
    count_rule = RuleResult(
        rule="hf_count_drop",
        description=f"{band} MHz {src} spots reflecting over {glat:+.0f}, {glon:+.0f}: "
        f"{f.n:.0f} in the hour vs usual {f.usual_n:.0f} ({pct:.0f}% of normal) "
        f"from about {f.usual_rx:.0f} receivers",
        value=f.z_n,
        threshold=-zmin,
        op="<=",
        passed=f.count_drop,
        baseline=f.usual_n,
    )
    snr_rule = RuleResult(
        rule="hf_snr_drop",
        description=f"{band} MHz median signal-to-noise {f.snr:.0f} dB "
        f"vs usual {f.usual_snr:.0f} dB"
        if f.snr is not None and f.usual_snr is not None
        else f"{band} MHz signal-to-noise: too few spots to compare",
        value=f.z_snr,
        threshold=-zmin,
        op="<=",
        passed=f.snr_drop,
        baseline=f.usual_snr,
    )
    rules = sorted([count_rule, snr_rule], key=lambda r: not r.passed)
    score = min(1.0, max(-(f.z_n or 0.0), -(f.z_snr or 0.0)) / (2 * zmin))
    conf = float(c["confidence_cap"]) * min(1.0, f.n_days / 10) * min(1.0, f.usual_rx / 6)
    edge_n = float(c["drop_ratio"]) * f.usual_n
    return Evidence(
        id=evidence_id(AlertKind.SPECTRUM_ANOMALY.value, cell, f"{src}|{band}", f.t0, aoi.name),
        kind=AlertKind.SPECTRUM_ANOMALY,
        detector=NAME,
        detector_version=VERSION,
        window_start=f.t0,
        window_end=f.t1,
        aoi=aoi.name,
        h3_cell=cell,
        h3_res=H3_RES,
        lat=glat,
        lon=glon,
        score=round(score, 3),
        rules=rules,
        features={
            "band_mhz": float(band),
            "n": f.n,
            "usual_n": f.usual_n,
            "z_n": float(f.z_n or 0.0),
            "snr_db": float(f.snr) if f.snr is not None else 0.0,
            "usual_snr_db": float(f.usual_snr) if f.usual_snr is not None else 0.0,
            "z_snr": float(f.z_snr or 0.0),
            "n_rx": f.n_rx,
            "usual_rx": f.usual_rx,
            "baseline_days": float(f.n_days),
            "network_ratio": f.net_ratio,
        },
        counterfactual=f"Would not fire with more than {edge_n:.0f} spots in the hour"
        + (
            f" and a signal-to-noise within {float(c['snr_drop_db']):.0f} dB of usual"
            if f.snr_drop
            else ""
        )
        + ", or with a flare on the sunlit path, a geomagnetic storm or a region-wide drop.",
        records=[RecordRef(source=src, id=i) for i in f.ids],
        coverage=Coverage(
            n_observed=int(f.usual_rx),
            n_quality=int(f.n_rx),
            providers=[src],
            confidence=round(conf, 3),
        ),
        gaps=[
            "checked: no M/X flare on the sunlit path, no geomagnetic storm, "
            "the network reporting normally elsewhere",
            "a drop can also be the usual receivers switched off: spots are aggregated, "
            "receivers are not identified",
            f"experimental HF indicator at the path midpoint (reflection region, "
            f"{float(c['cell_deg']):g}-degree cell); "
            "it never names an emitter type",
        ],
        created_at=now,
        bands=[Band.HF],
        persistence=persistence,
    )
