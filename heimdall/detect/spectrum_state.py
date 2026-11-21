"""Spectrum status per area and band (tasks 033/034). Pure: no I/O.

For every AOI and band one *indicator* is computed each tick from the victim receivers in that
band, then judged against that indicator's own history (robust z = (v - median) / (1.4826 MAD +
floor)) in the direction that means trouble:

| band | indicator | bad when |
|---|---|---|
| l1_gnss | % airborne ADS-B aircraft with degraded GPS (60 min) | high |
| l_1090 | aircraft heard (15 min) | low (receiver coverage) |
| vhf_marine | ships heard (60 min) | low (receiver coverage) |
| hf | WSPR/PSK spots with path midpoint over the area (60 min), same UTC hour baseline | low |
| sat_downlink | SatNOGS passes bad/failed near the area (6 h) | high |
| l_1400 | SMAP footprints flagged for RFI over the area (72 h) | high |
| c_sar | Sentinel-1 scenes over the area with RFI detected (72 h) | high |

States: no_coverage (too few receivers to judge), learning (baseline too short), normal,
elevated (z >= 2), anomalous (z >= 3). Natural explanations are attached, never hidden: a
geomagnetic storm (Kp) or solar radio blackout (R scale) for HF/GNSS. For sat_downlink,
l_1400 and c_sar an anomalous state also yields SPECTRUM_ANOMALY Evidence, experimental:
confidence capped, and it never names an emitter type. GNSS L1 and AIS already have dedicated
detectors (HF since P6: `detect/hf.py`, per path-midpoint cell), and low 1090/VHF counts are
receiver health, so those bands only feed the board.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from heimdall.detect.common import evidence_id
from heimdall.geo import KM_PER_NM, h3_cell, haversine_km
from heimdall.gnss_map import aggregate, counts_from_obs
from heimdall.schemas import (
    AircraftObs,
    AlertKind,
    Band,
    Coverage,
    Evidence,
    GeoEvent,
    RecordRef,
    RuleResult,
    VesselObs,
)

NAME = "spectrum_state"
VERSION = "spectrum-state-1.0.0"
METRIC = "band_indicator"
N_METRIC = "band_receivers"
State = Literal["no_coverage", "learning", "normal", "elevated", "anomalous"]

# per band: label, unit, direction (which way is trouble), min receivers, MAD floor, reach (km)
# All priors, not backtested: calibrate on the Baltic/Middle East controls and flare days.
INDICATORS: dict[Band, dict[str, Any]] = {
    Band.L1_GNSS: {
        "label": "aircraft with degraded GPS (1 h)",
        "unit": "%",
        "dir": "high",
        "min_n": 5,
        "mad_floor": 1.0,
        "reach_km": 0,
    },
    Band.L_1090: {
        "label": "aircraft heard (15 min)",
        "unit": "aircraft",
        "dir": "low",
        "min_n": 1,
        "mad_floor": 2.0,
        "reach_km": 0,
    },
    Band.VHF_MARINE: {
        "label": "ships heard (1 h)",
        "unit": "ships",
        "dir": "low",
        "min_n": 1,
        "mad_floor": 2.0,
        "reach_km": 0,
    },
    Band.HF: {
        "label": "HF spots reflecting over the area (1 h)",
        "unit": "spots",
        "dir": "low",
        "min_n": 5,
        "mad_floor": 3.0,
        "reach_km": 500,
        "hour_of_day": True,
    },
    Band.SAT_DOWNLINK: {
        "label": "satellite passes bad or failed (6 h)",
        "unit": "%",
        "dir": "high",
        "min_n": 3,
        "mad_floor": 5.0,
        "reach_km": 1500,
    },
    Band.L_1400: {
        "label": "SMAP footprints with interference (3 d)",
        "unit": "%",
        "dir": "high",
        "min_n": 10,
        "mad_floor": 2.0,
        "reach_km": 100,
    },
    Band.C_SAR: {
        "label": "Sentinel-1 scenes with interference (3 d)",
        "unit": "%",
        "dir": "high",
        "min_n": 1,
        "mad_floor": 10.0,
        "reach_km": 150,
    },
}
# HF evidence comes from detect/hf.py (P6, per path-midpoint cell); the board keeps the HF state
EVIDENCE_BANDS = (Band.SAT_DOWNLINK, Band.L_1400, Band.C_SAR)
# event kinds and look-back per band (the pipeline reads exactly these)
EVENT_WINDOWS: dict[str, timedelta] = {
    "hf_spots": timedelta(hours=1),
    "sat_observation": timedelta(hours=6),
    "l_band_rfi": timedelta(days=3),
    "sar_rfi": timedelta(days=3),
    "space_weather": timedelta(hours=6),
}
DEFAULTS: dict[str, Any] = {
    "baseline_days": 14,
    "min_samples": 288,  # one day of 5-min ticks before a state other than "learning"
    "min_samples_hourly": 36,  # hf: same-hour samples (3 days x 12 ticks)
    "elevated_z": 2.0,
    "anomalous_z": 3.0,
    "confidence_cap": 0.6,  # experimental single-band evidence stays <= medium
    "explained_factor": 0.5,
}


class AoiLike(Protocol):
    name: str
    lat: float
    lon: float
    radius_nm: float

    def contains(self, lat: float, lon: float) -> bool: ...


@dataclass
class Indicator:
    band: Band
    value: float | None
    n: int  # receivers / samples behind the value
    detail: dict[str, float] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)  # connector keys behind the value


@dataclass
class BandState:
    aoi: str
    band: Band
    state: State
    value: float | None
    n: int
    median: float | None = None
    z: float | None = None  # signed so that + means "towards trouble"
    sigma: float | None = None  # robust scale: 1.4826 MAD + floor
    n_history: int = 0
    explanations: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def _near(e: GeoEvent, aoi: AoiLike, reach_km: float) -> bool:
    if e.lat is None or e.lon is None:
        return False
    return haversine_km(e.lat, e.lon, aoi.lat, aoi.lon) <= aoi.radius_nm * KM_PER_NM + reach_km


def _num(v: Any) -> float:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0


def compute_indicators(
    aoi: AoiLike,
    obs: Sequence[AircraftObs],
    vessels: Sequence[VesselObs],
    events: Sequence[GeoEvent],
    now: datetime,
    nic_below: int = 7,
    nacp_below: int = 8,
) -> dict[Band, Indicator]:
    """One indicator per band for this AOI. `obs`/`vessels` are already inside the AOI."""
    out: dict[Band, Indicator] = {}
    h1, m15 = now - timedelta(hours=1), now - timedelta(minutes=15)
    air = [o for o in obs if o.ts >= h1 and not o.is_global]
    cells = aggregate(counts_from_obs(air, nic_below, nacp_below))
    total, bad = sum(c.total for c in cells), sum(c.bad for c in cells)
    out[Band.L1_GNSS] = Indicator(
        Band.L1_GNSS, 100.0 * bad / total if total else None, total, {"bad": bad, "total": total}
    )
    heard = {o.hex for o in obs if o.ts >= m15 and not o.is_global}
    out[Band.L_1090] = Indicator(Band.L_1090, float(len(heard)), len(heard))
    ships = {v.mmsi for v in vessels if v.ts >= h1 and not v.is_global}
    out[Band.VHF_MARINE] = Indicator(Band.VHF_MARINE, float(len(ships)), len(ships))

    def near(kind: str, band: Band) -> list[GeoEvent]:
        lo = now - EVENT_WINDOWS[kind]
        reach = float(INDICATORS[band]["reach_km"])
        return [e for e in events if e.kind == kind and e.ts >= lo and _near(e, aoi, reach)]

    hf = near("hf_spots", Band.HF)
    spots = sum(_num(e.attrs.get("n")) for e in hf)
    snr = [_num(e.attrs.get("snr_med")) for e in hf if e.attrs.get("snr_med") is not None]
    out[Band.HF] = Indicator(
        Band.HF,
        spots,
        int(spots),
        {"cells": len(hf), "snr_med": statistics.median(snr) if snr else 0.0},
    )
    sat = [
        e
        for e in near("sat_observation", Band.SAT_DOWNLINK)
        if e.attrs.get("status") in ("good", "bad", "failed")
    ]
    nbad = sum(1 for e in sat if e.attrs.get("status") in ("bad", "failed"))
    out[Band.SAT_DOWNLINK] = Indicator(
        Band.SAT_DOWNLINK,
        100.0 * nbad / len(sat) if sat else None,
        len(sat),
        {"bad_or_failed": nbad},
    )
    sm = near("l_band_rfi", Band.L_1400)
    fp = sum(_num(e.attrs.get("n_footprints")) for e in sm)
    rfi = sum(max(_num(e.attrs.get("n_rfi_h")), _num(e.attrs.get("n_rfi_v"))) for e in sm)
    out[Band.L_1400] = Indicator(
        Band.L_1400, 100.0 * rfi / fp if fp else None, int(fp), {"flagged": rfi}
    )
    sc = near("sar_rfi", Band.C_SAR)
    hit = sum(1 for e in sc if e.attrs.get("rfi_detected") is True)
    out[Band.C_SAR] = Indicator(
        Band.C_SAR, 100.0 * hit / len(sc) if sc else None, len(sc), {"with_rfi": hit}
    )
    for band, used in (
        (Band.HF, hf),
        (Band.SAT_DOWNLINK, sat),
        (Band.L_1400, sm),
        (Band.C_SAR, sc),
    ):
        out[band].sources = sorted({e.source for e in used})
    return out


def natural_explanations(band: Band, events: Iterable[GeoEvent], now: datetime) -> list[str]:
    """Space weather that can degrade HF or GNSS on its own (SWPC, last 6 h)."""
    if band not in (Band.HF, Band.L1_GNSS):
        return []
    sw = [e for e in events if e.kind == "space_weather" and e.ts >= now - timedelta(hours=6)]
    if not sw:
        return []
    kp = max(_num(e.attrs.get("kp")) for e in sw)
    r = max(_num(e.attrs.get("r_scale")) for e in sw)
    out = []
    if kp >= (5 if band == Band.HF else 7):
        out.append(
            f"geomagnetic storm (Kp {kp:g}) can disturb {'HF' if band == Band.HF else 'GPS'}"
        )
    if band == Band.HF and r >= 1:
        out.append(f"solar radio blackout (R{int(r)}) can black out daylit HF paths")
    return out


def history_for(band: Band, hist: Sequence[tuple[datetime, float]], now: datetime) -> list[float]:
    """Baseline samples; HF uses only samples within +-1 h of the same UTC hour (diurnal cycle)."""
    if not INDICATORS[band].get("hour_of_day"):
        return [v for _, v in hist]
    h = now.hour
    return [v for ts, v in hist if min((ts.hour - h) % 24, (h - ts.hour) % 24) <= 1]


def assess(
    aoi: str,
    ind: Indicator,
    history: Sequence[float],
    cfg: Mapping[str, Any] | None = None,
    explanations: Sequence[str] = (),
) -> BandState:
    c = {**DEFAULTS, **(cfg or {})}
    spec = INDICATORS[ind.band]
    st = BandState(
        aoi,
        ind.band,
        "no_coverage",
        ind.value,
        ind.n,
        n_history=len(history),
        explanations=list(explanations),
        sources=list(ind.sources),
    )
    if ind.value is None or ind.n < int(spec["min_n"]):
        return st
    need = int(c["min_samples_hourly"] if spec.get("hour_of_day") else c["min_samples"])
    if len(history) < need:
        st.state = "learning"
        return st
    med = statistics.median(history)
    mad = statistics.median(abs(v - med) for v in history)
    sigma = 1.4826 * mad + float(spec["mad_floor"])
    z = (ind.value - med) / sigma
    bad_z = z if spec["dir"] == "high" else -z
    st.median, st.z, st.sigma = med, round(bad_z, 2), sigma
    st.state = (
        "anomalous"
        if bad_z >= c["anomalous_z"]
        else "elevated"
        if bad_z >= c["elevated_z"]
        else "normal"
    )
    return st


def evidence(
    s: BandState,
    aoi: AoiLike,
    now: datetime,
    window: tuple[datetime, datetime],
    cfg: Mapping[str, Any] | None = None,
) -> Evidence | None:
    """SPECTRUM_ANOMALY Evidence for an anomalous experimental band."""
    c = {**DEFAULTS, **(cfg or {})}
    if s.state != "anomalous" or s.band not in EVIDENCE_BANDS or s.z is None or s.value is None:
        return None
    spec = INDICATORS[s.band]
    high = spec["dir"] == "high"
    cell = h3_cell(aoi.lat, aoi.lon, 4)
    thr = float(c["anomalous_z"])
    conf = min(float(c["confidence_cap"]), s.n / (4.0 * float(spec["min_n"])))
    if s.explanations:
        conf *= float(c["explained_factor"])
    # value at which z would fall below the alert threshold (counterfactual)
    sig = s.sigma or 0.0
    edge = (s.median or 0.0) + (thr * sig if high else -thr * sig)
    return Evidence(
        id=evidence_id(AlertKind.SPECTRUM_ANOMALY.value, cell, s.band.value, window[0], aoi.name),
        kind=AlertKind.SPECTRUM_ANOMALY,
        detector=NAME,
        detector_version=VERSION,
        window_start=window[0],
        window_end=window[1],
        aoi=aoi.name,
        h3_cell=cell,
        h3_res=4,
        lat=aoi.lat,
        lon=aoi.lon,
        score=min(1.0, s.z / (2 * thr)),
        rules=[
            RuleResult(
                rule=f"{s.band.value}_indicator_z",
                description=f"{spec['label']}: {s.value:.1f} {spec['unit']} vs typical "
                f"{(s.median or 0):.1f} ({'higher' if high else 'lower'} than usual)",
                value=round(s.z, 2),
                threshold=thr,
                op=">=",
                passed=True,
                baseline=s.median,
            )
        ],
        features={
            "value": float(s.value),
            "baseline": float(s.median or 0.0),
            "z": float(s.z),
            "n": float(s.n),
        },
        counterfactual=f"Would not fire if the value were {'below' if high else 'above'} "
        f"about {edge:.1f} {spec['unit']}.",
        records=[
            RecordRef(source=src, id=f"{s.band.value}@{window[1]:%Y%m%dT%H%M}") for src in s.sources
        ],
        coverage=Coverage(
            n_observed=s.n, n_quality=s.n, providers=s.sources, confidence=round(conf, 3)
        ),
        gaps=[*s.explanations, "experimental indicator: it never names an emitter type"],
        created_at=now,
        bands=[s.band],
    )
