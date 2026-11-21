"""HF reception anomaly at the RECEIVER (P6, task 060). Experimental. Pure.

Why a second HF indicator: the path-midpoint detector watches where signals reflect, which
spreads a local noise source over many midpoint cells. A jammer or strong local interference
raises the noise floor at the receivers near it, so every signal they hear, from every direction,
arrives weaker, and fewer are decoded. That is seen best per receiver cell.

Input: `hf_rx` GeoEvents (WSPR Live, PSK Reporter): spots per 10-min bin, band and receiver grid
cell (`rx_grid_deg`, 2 degrees), with distinct receivers and median SNR. Callsigns never stored.
For each (source, band, receiver cell) and a 60-min window, compared with the same clock window
on each of the previous `baseline_days` days (HF follows the sun):
Day-to-day HF swings at the same hour are large (median SNR spread ~2-3 dB, decodes x0.3-x3)
but mostly shared by a whole region, so the main test is RELATIVE: the cell minus the median of
its neighbouring receiver cells (same source and band, within neighbour_km) in the same hour,
against that same difference on each baseline day. A local source moves one cell; propagation
moves all of them.
* R1 SNR drop: relative median SNR fell by >= snr_drop_db with robust z <= -z_min, and the cell
  itself fell >= snr_drop_db / 2 (not neighbours rising). >= min_rx receivers now and usually.
* R2 decode drop: relative spots per active receiver <= rate_ratio x usual, z <= -z_min, and
  the cell's own ratio <= (1 + rate_ratio) / 2. Per active receiver, so stations switching off
  do not count; stations still on but deaf do.
* R3 local (by construction of R1/R2). A cell with no neighbours falls back to the absolute
  same-hour test with the stricter z_min_alone and lower confidence; stated in the Evidence.
* R4 wideband (support only): other bands in the same cell dropped too (a local noise source is
  usually broadband; a band closing is not).
Also explained away, as in 045: M1+ flare with the receiver cell sunlit, Kp >= kp_explain, the
source's spot total for the whole box < network_ok_ratio x usual. Persistence = consecutive
flagged windows `step_min` apart (fusion requires 2). Confidence is capped (single experimental
band, <= medium) and the alert never names an emitter type.
Thresholds are priors from the WSPR receiver-cell backfill 2026-09-17..10-03 (task 060 record).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from heimdall.detect import hf
from heimdall.detect.common import MAD_SCALE, evidence_id
from heimdall.geo import h3_cell, haversine_km
from heimdall.schemas import (
    AlertKind,
    Band,
    Coverage,
    Evidence,
    GeoEvent,
    RecordRef,
    RuleResult,
)

NAME = "hf_rx"
VERSION = "hf-rx-0.1.0"
KIND = "hf_rx"
CONTEXT_KINDS = hf.CONTEXT_KINDS
H3_RES = 3
Key = hf.Key
SpotBin = hf.SpotBin

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "cell_deg": 0.0,  # 0 = keep the connector's receiver cells (rx_grid_deg)
    "window_min": 60,
    "step_min": 30,
    "persist_windows": 3,
    "settle_min": 20,
    "baseline_days": 14,
    "min_baseline_days": 5,
    "min_rx": 2,
    "min_spots": 20,  # usual spots per window before a drop means anything
    "snr_min_spots": 10,
    "snr_drop_db": 6.0,
    "snr_mad_floor_db": 1.5,
    "rate_ratio": 0.4,
    "rate_mad_floor_frac": 0.15,
    "rate_mad_floor_log": 0.15,
    "z_min": 2.0,  # relative test (cell minus neighbours): shared swings cancel
    "z_min_alone": 3.0,  # absolute test when a cell has no neighbours
    "neighbour_km": 1500,
    "wide_db": 3.0,
    "wide_ratio": 0.6,
    "flare_flux": 1e-5,
    "flare_lead_min": 30,
    "sunlit_elev_deg": -6.0,
    "kp_explain": 5.0,
    "network_ok_ratio": 0.6,
    "reach_km": 300,  # receiver cells this far beyond an area's edge count for it
    "confidence_cap": 0.5,
}


def bins_from_events(events: Iterable[GeoEvent]) -> list[SpotBin]:
    return hf.bins_from_events(events, KIND)


def newest_window_end(now: datetime, cfg: Mapping[str, Any] | None = None) -> datetime:
    return hf.newest_window_end(now, {**DEFAULTS, **(cfg or {})})


def span(cfg: Mapping[str, Any] | None = None) -> timedelta:
    return hf.span({**DEFAULTS, **(cfg or {})})


@dataclass
class Change:
    """One key's state in a window vs its usual, plus per-baseline-day values (for neighbours)."""

    n: float
    usual_n: float
    n_rx: float
    usual_rx: float
    snr: float | None  # now, None when too few spots to compare
    usual_snr: float | None
    d_snr: float | None
    z_snr: float | None
    rate: float | None  # spots per active receiver now
    rate_ratio: float | None
    z_rate: float | None
    n_days: int
    snr_days: list[float | None] = field(default_factory=list)  # aligned with baseline days
    rate_days: list[float | None] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)


@dataclass
class Relative:
    """The cell's change minus its neighbours' change (same hour), against 14 days of the same."""

    d_snr: float | None = None
    z_snr: float | None = None
    rate_ratio: float | None = None
    z_rate: float | None = None
    n_days: int = 0


@dataclass
class RxFlag:
    key: Key
    t0: datetime
    t1: datetime
    ch: Change
    rel: Relative | None  # None = no neighbour data: absolute test only
    snr_drop: bool
    rate_drop: bool
    nb_d_snr: float | None
    nb_ratio: float | None
    n_nb: int
    bands_down: list[int]
    net_ratio: float
    explained: list[str]

    @property
    def local(self) -> bool | None:
        return None if self.rel is None else True

    @property
    def flagged(self) -> bool:
        return (self.snr_drop or self.rate_drop) and not self.explained


@dataclass
class RxState:
    windows: list[tuple[datetime, datetime]]
    flags: list[dict[Key, RxFlag]]
    created: datetime


def _changes(
    cur: Mapping[Key, hf.Agg],
    cur_tot: Mapping[str, float],
    days: Sequence[tuple[dict[Key, hf.Agg], dict[str, float]]],
    c: Mapping[str, Any],
) -> dict[Key, Change]:
    out: dict[Key, Change] = {}
    min_snr_n, min_days = float(c["snr_min_spots"]), int(c["min_baseline_days"])
    for key in set(cur) | {k for aggs, _ in days for k in aggs}:
        src = key[0]
        on = [tot.get(src, 0.0) > 0 for _, tot in days]
        active = [aggs for (aggs, _), o in zip(days, on, strict=True) if o]
        if len(active) < min_days or cur_tot.get(src, 0.0) <= 0:
            continue
        usual_n = statistics.median(a[key].n if key in a else 0.0 for a in active)
        if usual_n < float(c["min_spots"]):
            continue
        now = cur.get(key, hf.Agg())
        usual_rx = statistics.median(a[key].n_rx if key in a else 0.0 for a in active)
        snr_days: list[float | None] = []
        rate_days: list[float | None] = []
        for (a, _), o in zip(days, on, strict=True):
            g = a.get(key) if o else None
            snr_days.append(g.snr if g is not None and g.n >= min_snr_n else None)
            rate_days.append(g.n / g.n_rx if g is not None and g.n_rx > 0 else None)
        snr_now = now.snr if now.n >= min_snr_n else None
        rate_now = now.n / now.n_rx if now.n_rx > 0 else None
        snrs = [x for x in snr_days if x is not None]
        usual_snr = d_snr = z_snr = None
        if len(snrs) >= min_days:
            usual_snr, mad_s = hf._robust(snrs)
            if snr_now is not None:
                d_snr = snr_now - usual_snr
                z_snr = d_snr / (MAD_SCALE * mad_s + float(c["snr_mad_floor_db"]))
        rates = [x for x in rate_days if x is not None]
        ratio = z_rate = None
        if len(rates) >= min_days and rate_now is not None:
            usual_rate, mad_r = hf._robust(rates)
            if usual_rate > 0:
                ratio = rate_now / usual_rate
                sig = MAD_SCALE * mad_r + float(c["rate_mad_floor_frac"]) * usual_rate
                z_rate = (rate_now - usual_rate) / sig
        out[key] = Change(
            now.n,
            usual_n,
            now.n_rx,
            usual_rx,
            snr_now,
            usual_snr,
            d_snr,
            z_snr,
            rate_now,
            ratio,
            z_rate,
            len(active),
            snr_days,
            rate_days,
            now.ids[:6],
        )
    return out


def _med(xs: Iterable[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return statistics.median(v) if v else None


def relative(ch: Change, nbs: Sequence[Change], c: Mapping[str, Any]) -> Relative | None:
    """Cell minus neighbour median, now vs the same difference on each baseline day. Shared
    propagation swings cancel, so a local drop stands out at a few dB. None without neighbours."""
    if not nbs:
        return None
    min_days = int(c["min_baseline_days"])
    out = Relative()
    rel_s: list[float] = []
    rel_r: list[float] = []
    for i in range(len(ch.snr_days)):
        nb = _med(n.snr_days[i] for n in nbs)
        if ch.snr_days[i] is not None and nb is not None:
            rel_s.append(float(ch.snr_days[i]) - nb)  # type: ignore[arg-type]
        nr = _med(n.rate_days[i] for n in nbs)
        cr = ch.rate_days[i]
        if cr is not None and nr is not None and cr > 0 and nr > 0:
            rel_r.append(math.log(cr / nr))
    nb_now = _med(n.snr for n in nbs)
    if ch.snr is not None and nb_now is not None and len(rel_s) >= min_days:
        med, mad = hf._robust(rel_s)
        out.d_snr = (ch.snr - nb_now) - med
        out.z_snr = out.d_snr / (MAD_SCALE * mad + float(c["snr_mad_floor_db"]))
        out.n_days = len(rel_s)
    nr_now = _med(n.rate for n in nbs)
    if ch.rate and nr_now and len(rel_r) >= min_days:
        med, mad = hf._robust(rel_r)
        lr = math.log(ch.rate / nr_now) - med
        out.rate_ratio = math.exp(lr)
        out.z_rate = lr / (MAD_SCALE * mad + float(c["rate_mad_floor_log"]))
        out.n_days = max(out.n_days, len(rel_r))
    if out.d_snr is None and out.rate_ratio is None:
        return None
    return out


def _drops(ch: Change, rel: Relative | None, c: Mapping[str, Any]) -> tuple[bool, bool]:
    """Relative test when neighbours exist (the cell must also be down itself, not neighbours
    up); otherwise the absolute same-hour test, with a stricter z."""
    min_rx = float(c["min_rx"])
    if not (ch.n_rx >= min_rx and ch.usual_rx >= min_rx):
        return False, False
    drop_db, ratio = float(c["snr_drop_db"]), float(c["rate_ratio"])
    if rel is not None:
        z = float(c["z_min"])
        snr = (
            rel.d_snr is not None
            and rel.z_snr is not None
            and rel.d_snr <= -drop_db
            and rel.z_snr <= -z
            and ch.d_snr is not None
            and ch.d_snr <= -drop_db / 2
        )
        rate = (
            rel.rate_ratio is not None
            and rel.z_rate is not None
            and rel.rate_ratio <= ratio
            and rel.z_rate <= -z
            and ch.rate_ratio is not None
            and ch.rate_ratio <= (1 + ratio) / 2
        )
        return snr, rate
    z = float(c["z_min_alone"])
    snr = ch.d_snr is not None and ch.z_snr is not None and ch.d_snr <= -drop_db and ch.z_snr <= -z
    rate = (
        ch.rate_ratio is not None
        and ch.z_rate is not None
        and ch.rate_ratio <= ratio
        and ch.z_rate <= -z
    )
    return snr, rate


def assess_window(
    by_bin: Mapping[datetime, Sequence[SpotBin]],
    t0: datetime,
    t1: datetime,
    context: Sequence[GeoEvent],
    c: Mapping[str, Any],
) -> dict[Key, RxFlag]:
    def window(lo: datetime, hi: datetime) -> list[SpotBin]:
        return [b for t, bs in by_bin.items() if lo <= t < hi for b in bs]

    cur, cur_tot = hf.aggregate(window(t0, t1))
    days = []
    for d in range(1, int(c["baseline_days"]) + 1):
        shift = timedelta(days=d)
        days.append(hf.aggregate(window(t0 - shift, t1 - shift)))
    changes = _changes(cur, cur_tot, days, c)
    flare, storm = hf._space_weather(context, t0, t1, c)
    mid = t0 + (t1 - t0) / 2
    nb_km = float(c["neighbour_km"])
    by_sb: dict[tuple[str, int], list[Key]] = defaultdict(list)
    for k in changes:
        by_sb[(k[0], k[1])].append(k)
    out: dict[Key, RxFlag] = {}
    for key, ch in changes.items():
        src, band, lat, lon = key
        nbs = [
            changes[k]
            for k in by_sb[(src, band)]
            if k != key and haversine_km(lat, lon, k[2], k[3]) <= nb_km
        ]
        rel = relative(ch, nbs, c)
        snr_drop, rate_drop = _drops(ch, rel, c)
        if not (snr_drop or rate_drop):
            continue
        bands_down = sorted(
            k[1]
            for k, v in changes.items()
            if k[0] == src
            and k[1] != band
            and (k[2], k[3]) == (lat, lon)
            and (
                (v.d_snr is not None and v.d_snr <= -float(c["wide_db"]))
                or (v.rate_ratio is not None and v.rate_ratio <= float(c["wide_ratio"]))
            )
        )
        active_tot = [tot.get(src, 0.0) for _, tot in days if tot.get(src, 0.0) > 0]
        usual_tot = statistics.median(active_tot) if active_tot else 0.0
        net_ratio = cur_tot.get(src, 0.0) / usual_tot if usual_tot else 0.0
        explained = []
        if flare and hf.sun_elevation_deg(lat, lon, mid) >= float(c["sunlit_elev_deg"]):
            explained.append(flare)
        if storm:
            explained.append(storm)
        if net_ratio < float(c["network_ok_ratio"]):
            explained.append(
                f"{src} reported {net_ratio:.0%} of its usual spots over the whole region "
                "(feed or region-wide propagation, not a local effect)"
            )
        nb_d = _med(n.d_snr for n in nbs)
        nb_r = _med(n.rate_ratio for n in nbs)
        out[key] = RxFlag(
            key,
            t0,
            t1,
            ch,
            rel,
            snr_drop,
            rate_drop,
            None if nb_d is None else round(nb_d, 2),
            None if nb_r is None else round(nb_r, 3),
            len(nbs),
            bands_down,
            round(net_ratio, 3),
            explained,
        )
    return out


def prepare(
    bins: Iterable[SpotBin],
    context: Sequence[GeoEvent],
    t_end: datetime,
    cfg: Mapping[str, Any] | None = None,
    created: datetime | None = None,
) -> RxState:
    """Assess the newest window ending `t_end` and the `persist_windows - 1` before it."""
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
    return RxState(wins, flags, created or t_end)


def detect(
    state: RxState,
    aoi: hf.AoiLike,
    cfg: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> list[Evidence]:
    """SPECTRUM_ANOMALY (band HF) Evidence for flagged, unexplained receiver cells near `aoi`."""
    c = {**DEFAULTS, **(cfg or {})}
    out: list[Evidence] = []
    for key, f in state.flags[0].items():
        if not f.flagged or not hf._near(key, aoi, float(c["reach_km"])):
            continue
        persistence = 0
        for w in state.flags:
            if key in w and w[key].flagged:
                persistence += 1
            else:
                break
        out.append(_evidence(f, persistence, aoi, c, now or state.created))
    return out


def _r2(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def _evidence(
    f: RxFlag, persistence: int, aoi: hf.AoiLike, c: Mapping[str, Any], now: datetime
) -> Evidence:
    src, band, glat, glon = f.key
    ch = f.ch
    cell = h3_cell(glat, glon, H3_RES)
    zmin = float(c["z_min"] if f.rel is not None else c["z_min_alone"])
    where = f"receivers near {glat:+.0f}, {glon:+.0f}"
    snr_rule = RuleResult(
        rule="hf_rx_snr_drop",
        description=f"{band} MHz {src} {where}: median signal-to-noise {ch.snr:.0f} dB vs usual "
        f"{ch.usual_snr:.0f} dB for this time of day, all directions"
        if ch.snr is not None and ch.usual_snr is not None
        else f"{band} MHz signal-to-noise: too few spots to compare",
        value=_r2(f.rel.z_snr if f.rel is not None else ch.z_snr),  # the z the decision used
        threshold=-zmin,
        op="<=",
        passed=f.snr_drop,
        baseline=ch.usual_snr,
    )
    pct = 100.0 * (ch.rate_ratio or 0.0)
    rate_rule = RuleResult(
        rule="hf_rx_decode_drop",
        description=f"{band} MHz {where}: each listening receiver decoded {pct:.0f}% of its usual "
        f"number of signals ({ch.n:.0f} spots from {ch.n_rx:.0f} receivers vs usual "
        f"{ch.usual_n:.0f} from {ch.usual_rx:.0f})",
        value=_r2(f.rel.z_rate if f.rel is not None else ch.z_rate),
        threshold=-zmin,
        op="<=",
        passed=f.rate_drop,
        baseline=1.0,
    )
    nb_km = float(c["neighbour_km"])
    rel = f.rel
    if rel is None:
        local_rule = RuleResult(
            rule="hf_rx_local",
            description=f"no other receiver cells within {nb_km:.0f} km to compare: local or "
            f"region-wide unknown (stricter z {float(c['z_min_alone']):g} applied instead)",
            value=None,
            threshold=-zmin,
            op="<=",
            passed=False,
        )
    else:
        parts = []
        if rel.d_snr is not None:
            parts.append(f"signal-to-noise {rel.d_snr:+.1f} dB")
        if rel.rate_ratio is not None:
            parts.append(f"decodes {100 * rel.rate_ratio:.0f}%")
        nb = []
        if f.nb_d_snr is not None:
            nb.append(f"{f.nb_d_snr:+.1f} dB")
        if f.nb_ratio is not None:
            nb.append(f"{100 * f.nb_ratio:.0f}% of usual decodes")
        local_rule = RuleResult(
            rule="hf_rx_local",
            description=f"compared with {f.n_nb} neighbouring receiver cell(s) within "
            f"{nb_km:.0f} km in the same hour ({', '.join(nb) or 'no change'} there): "
            f"{', '.join(parts)} relative to its usual difference over {rel.n_days} days "
            "(local, not a region-wide propagation change)",
            value=None if rel.z_snr is None else round(rel.z_snr, 2),
            threshold=-zmin,
            op="<=",
            passed=True,
        )
    wide_rule = RuleResult(
        rule="hf_rx_wideband",
        description=(
            f"other bands at these receivers dropped too: {', '.join(map(str, f.bands_down))} MHz"
            if f.bands_down
            else "no other band at these receivers dropped (support only)"
        ),
        value=float(len(f.bands_down)),
        threshold=1.0,
        op=">=",
        passed=bool(f.bands_down),
    )
    rules = sorted([snr_rule, rate_rule, local_rule, wide_rule], key=lambda r: not r.passed)
    zs = [ch.z_snr, ch.z_rate] if rel is None else [rel.z_snr, rel.z_rate]
    z = max(-(x or 0.0) for x in zs)
    score = min(1.0, z / (2 * zmin) + (0.15 if f.bands_down else 0.0))
    conf = float(c["confidence_cap"]) * min(1.0, ch.n_days / 10) * min(1.0, ch.usual_rx / 4)
    if f.local is None:
        conf *= 0.6
    return Evidence(
        id=evidence_id(AlertKind.SPECTRUM_ANOMALY.value, cell, f"rx|{src}|{band}", f.t0, aoi.name),
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
            "n": ch.n,
            "usual_n": ch.usual_n,
            "n_rx": ch.n_rx,
            "usual_rx": ch.usual_rx,
            "snr_db": float(ch.snr) if ch.snr is not None else 0.0,
            "usual_snr_db": float(ch.usual_snr) if ch.usual_snr is not None else 0.0,
            "d_snr_db": float(ch.d_snr or 0.0),
            "z_snr": float(ch.z_snr or 0.0),
            "decode_ratio": float(ch.rate_ratio) if ch.rate_ratio is not None else 1.0,
            "z_decode": float(ch.z_rate or 0.0),
            "neighbours": float(f.n_nb),
            "rel_d_snr_db": float(rel.d_snr) if rel and rel.d_snr is not None else 0.0,
            "rel_decode_ratio": float(rel.rate_ratio) if rel and rel.rate_ratio else 1.0,
            "neighbour_d_snr_db": float(f.nb_d_snr) if f.nb_d_snr is not None else 0.0,
            "bands_down": float(len(f.bands_down)),
            "baseline_days": float(ch.n_days),
            "network_ratio": f.net_ratio,
        },
        counterfactual=(
            f"Would not fire with a signal-to-noise within {float(c['snr_drop_db']):.0f} dB of "
            f"usual and decodes above {100 * float(c['rate_ratio']):.0f}% of usual per receiver, "
            f"or if receivers within {nb_km:.0f} km dropped the same way (propagation), or with a "
            "flare on the sunlit side, a geomagnetic storm or a region-wide drop."
        ),
        records=[RecordRef(source=src, id=i) for i in ch.ids],
        coverage=Coverage(
            n_observed=int(ch.usual_rx),
            n_quality=int(ch.n_rx),
            providers=[src],
            confidence=round(conf, 3),
        ),
        gaps=[
            "checked: no M/X flare with these receivers sunlit, no geomagnetic storm, the "
            "network reporting normally elsewhere",
            "receivers are counted, not identified: one station's own fault looks the same if "
            "it dominates the cell",
            f"experimental HF indicator at the receivers ({glat:+.0f}, {glon:+.0f}, about "
            "200 km cell); a raised noise floor can be any local interference (power lines, "
            "electronics) as well as jamming; it never names an emitter type",
        ],
        created_at=now,
        bands=[Band.HF],
        persistence=persistence,
    )
