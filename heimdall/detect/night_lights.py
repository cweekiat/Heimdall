"""Night-lights drop detector over Black Marble `night_lights` GeoEvents (per r4 cell, per night).

Assumptions (ADR-007, task 021b)
--------------------------------
* One event per (r4 cell, night): `value` = mean radiance (nW/cm^2/sr) over good-quality,
  cloud-free pixels only, `attrs.date` = night (ISO), `attrs.valid_frac` = share of usable pixels.
  Rasters are reduced upstream; no pixel data is seen here.
* A night is *valid* when `valid_frac >= min_valid_frac` (N3). Invalid (cloudy) nights carry no
  information: they never count as dropped, never enter a baseline, and are skipped inside a run.
* Forward pass over valid nights: `clean` holds nights not flagged as dropped. For each night the
  baseline B = median of the last `baseline_nights` clean nights; the night is *dropped* when
  B >= `min_baseline_radiance` (N1, lit areas only) and value / B <= `drop_ratio_max` (N2). A
  dropped night is not added to `clean` (so an outage cannot poison its own baseline); a
  non-dropped night resets the run. The run at the tail is the candidate; N4 needs
  `>= min_nights` consecutive dropped valid nights and the latest reported night must itself be
  valid (else no Evidence). Slow dimming (-10 %) never reaches the 0.5 ratio; a permanent change
  keeps alerting until the baseline is reset upstream.
* Imagery lags 1-5 days: Evidence always carries gap "imagery lag: N days"; nothing is
  emitted when the latest night is older than `max_lag_days`. A low-`valid_frac` latest night gives
  no Evidence (gap only, i.e. nothing is returned).
* Text is never read. Persistence is fusion's job.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from heimdall.detect.common import cfg_get, evidence_id, median
from heimdall.geo import KM_PER_NM, h3_center, haversine_km
from heimdall.schemas import AlertKind, Coverage, Evidence, GeoEvent, RecordRef, RuleResult

NAME = "night_lights"
VERSION = "night-lights-1.0.0"

DEFAULTS: dict[str, Any] = {
    "baseline_nights": 30,
    "min_valid_frac": 0.5,
    "min_baseline_radiance": 5.0,
    "drop_ratio_max": 0.5,
    "min_nights": 2,
    "min_history_nights": 5,
    "max_lag_days": 10,
    "aoi_margin_km": 25,
    "h3_res": 4,
    "max_records": 50,
}


class AoiLike(Protocol):
    name: str
    lat: float
    lon: float
    radius_nm: float


def _cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg_get(cfg, k, v) for k, v in DEFAULTS.items()}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _night(e: GeoEvent) -> date:
    try:
        return date.fromisoformat(str(e.attrs.get("date"))[:10])
    except ValueError:
        return e.ts.date()


def _valid_frac(e: GeoEvent) -> float:
    v = e.attrs.get("valid_frac")
    return float(v) if isinstance(v, (int, float)) else 0.0


def detect(
    events: Sequence[GeoEvent], cfg: dict[str, Any], now: datetime, *, aoi: AoiLike
) -> list[Evidence]:
    """One OUTAGE Evidence per r4 cell whose lights dropped for >= `min_nights` valid nights."""
    c = _cfg(cfg)
    lim_km = aoi.radius_nm * KM_PER_NM + c["aoi_margin_km"]
    today = now.date()
    cells: dict[str, dict[date, GeoEvent]] = defaultdict(dict)
    for e in events:
        if e.kind != "night_lights" or e.value is None:
            continue
        cell = str(e.attrs.get("h3_r4") or e.h3_r4 or "")
        if not cell:
            continue
        d = _night(e)
        if d > today:
            continue
        prev = cells[cell].get(d)
        if prev is None or _valid_frac(e) > _valid_frac(prev):
            cells[cell][d] = e  # duplicates (re-fetches/products): keep the best-quality one

    out: list[Evidence] = []
    for cell, by_date in sorted(cells.items()):
        lat, lon = h3_center(cell)
        if haversine_km(lat, lon, aoi.lat, aoi.lon) > lim_km:
            continue
        nights = sorted(by_date)
        latest = nights[-1]
        lag = (today - latest).days
        if lag > c["max_lag_days"] or _valid_frac(by_date[latest]) < c["min_valid_frac"]:
            continue  # N3 fails on the latest night: gap only, no Evidence
        clean: list[float] = []
        run: list[tuple[date, float, float]] = []  # (night, ratio, valid_frac)
        run_base: float | None = None
        n_excluded = 0
        for d in nights:
            e = by_date[d]
            if _valid_frac(e) < c["min_valid_frac"]:
                n_excluded += 1
                continue
            v = float(e.value or 0.0)
            hist = clean[-c["baseline_nights"] :]
            b = median(hist) if len(hist) >= c["min_history_nights"] else 0.0
            if b >= c["min_baseline_radiance"] and v / b <= c["drop_ratio_max"]:
                if not run:
                    run_base = b
                run.append((d, v / b, _valid_frac(e)))
            else:
                run = []
                run_base = None
                clean.append(v)
        if not run or run[-1][0] != latest or run_base is None:
            continue
        n_run = len(run)
        if n_run < c["min_nights"]:
            continue
        worst = max(r for _, r, _ in run)
        mean_vf = sum(v for _, _, v in run) / n_run
        n_hist = min(len(clean), c["baseline_nights"])
        cov = _clamp(n_hist / c["baseline_nights"]) * mean_vf
        w_start = datetime.combine(run[0][0], time.min, tzinfo=UTC)
        w_end = datetime.combine(latest + timedelta(days=1), time.min, tzinfo=UTC)
        run_ids = {by_date[d].id for d, _, _ in run}
        base_recs = [by_date[d] for d in nights if by_date[d].id not in run_ids][-20:]
        recs = sorted([by_date[d] for d, _, _ in run], key=lambda e: e.id) + base_recs
        gaps = [f"imagery lag: {lag} days"]
        if n_excluded:
            gaps.append(f"{n_excluded} nights excluded (low valid fraction)")
        if n_hist < c["baseline_nights"]:
            gaps.append(f"short baseline: {n_hist} of {c['baseline_nights']} nights")
        score = _clamp(1.0 - worst) * _clamp(n_run / (2 * c["min_nights"]))
        out.append(
            Evidence(
                id=evidence_id(AlertKind.OUTAGE.value, cell, "night_lights", w_start, aoi.name),
                kind=AlertKind.OUTAGE,
                detector=NAME,
                detector_version=VERSION,
                aoi=aoi.name,
                window_start=w_start,
                window_end=w_end,
                h3_cell=cell,
                h3_res=c["h3_res"],
                lat=lat,
                lon=lon,
                score=score,
                rules=[
                    RuleResult(
                        rule="N1_lit_baseline",
                        description="Baseline radiance (median of prior clean nights), nW/cm2/sr",
                        value=run_base,
                        threshold=float(c["min_baseline_radiance"]),
                        op=">=",
                        passed=True,
                        baseline=run_base,
                    ),
                    RuleResult(
                        rule="N2_drop_ratio",
                        description="Worst night radiance / baseline in the dropped run",
                        value=worst,
                        threshold=float(c["drop_ratio_max"]),
                        op="<=",
                        passed=True,
                        baseline=run_base,
                    ),
                    RuleResult(
                        rule="N3_valid_fraction",
                        description="Lowest valid-pixel fraction in the dropped run",
                        value=min(v for _, _, v in run),
                        threshold=float(c["min_valid_frac"]),
                        op=">=",
                        passed=True,
                    ),
                    RuleResult(
                        rule="N4_consecutive_nights",
                        description="Consecutive valid nights below the drop ratio",
                        value=float(n_run),
                        threshold=float(c["min_nights"]),
                        op=">=",
                        passed=True,
                    ),
                ],
                features={
                    "night_lights_ratio": run[-1][1],
                    "worst_ratio": worst,
                    "baseline_radiance": run_base,
                    "n_dropped_nights": float(n_run),
                    "imagery_lag_days": float(lag),
                },
                records=[RecordRef(source=e.source, id=e.id, url=e.url) for e in recs][
                    : c["max_records"]
                ],
                counterfactual=(
                    f"would not fire with < {c['min_nights']} consecutive valid nights at or "
                    f"below {c['drop_ratio_max']:g} of baseline, an unlit cell "
                    f"(< {c['min_baseline_radiance']:g}) or valid fraction "
                    f"< {c['min_valid_frac']:g}"
                ),
                coverage=Coverage(
                    n_observed=len(clean[-c["baseline_nights"] :]) + n_run,
                    n_quality=n_run,
                    providers=sorted({e.source for e in recs}),
                    confidence=cov,
                ),
                gaps=gaps,
                created_at=now,
            )
        )
    return out
