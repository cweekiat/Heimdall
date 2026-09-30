"""`python -m heimdall.backtest` — replay one AOI-day of trace files, score vs GPSJam, sweep
jamming thresholds and write `reports/backtest-<aoi>-<date>.md`.

Offline only: reads local trace files, a local GPSJam CSV and config/*.yaml. The report is a pure
function of those inputs (no wall-clock time, sorted output), so re-runs are byte-identical.
Threshold changes are *proposed* in the report; config/config.yaml is never modified.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from heimdall import config
from heimdall.backtest.replay import ReplayResult, replay
from heimdall.backtest.score import (
    LEVELS,
    PREDS,
    GpsJamCell,
    LeadTime,
    ScoreReport,
    daily_predictions,
    lead_times,
    load_gpsjam,
    score,
)
from heimdall.backtest.traces import PROVIDER, iter_trace_dir, trace_files
from heimdall.config import AOI, Settings
from heimdall.detect import jamming, orbit, spoofing
from heimdall.detect.common import cell_of
from heimdall.fuse.admiralty import grades_from_sources

RATIO_ALERTS = (0.05, 0.08, 0.10, 0.15)
MIN_AIRCRAFT = (3, 5, 8)
#: Proposed sparse-traffic jamming profile for the SE Asia AOIs (per-AOI `detectors` override).
SPARSE_PROFILE: dict[str, Any] = {"window_min": 60, "min_aircraft": 3, "confidence_cap": 0.5}
TOTAL_BUCKETS = ((1, 2), (3, 4), (5, 7), (8, 14), (15, None))


@dataclass
class Run:
    label: str
    ratio_alert: float
    min_aircraft: int
    result: ReplayResult
    score: ScoreReport | None

    @property
    def n_alerts(self) -> int:
        return len(self.result.jamming_alerts())


def _fmt(x: float | None, nd: int = 3) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _variant(base: Mapping[str, dict[str, Any]], **jam: Any) -> dict[str, dict[str, Any]]:
    """Copy of the effective detector config with jamming keys replaced. ratio_clear keeps its
    configured proportion to ratio_alert so the hysteresis band stays below the trigger."""
    det = copy.deepcopy(dict(base))
    j = det.setdefault("jamming", {})
    alert0 = float(j.get("ratio_alert", jamming.DEFAULTS["ratio_alert"]))
    clear0 = float(j.get("ratio_clear", jamming.DEFAULTS["ratio_clear"]))
    j.update(jam)
    if "ratio_alert" in jam:
        j["ratio_clear"] = round(float(jam["ratio_alert"]) * clear0 / alert0, 4)
    return det


def _run(
    label: str,
    obs: Sequence[Any],
    settings: Settings,
    aoi: AOI,
    day: tuple[datetime, datetime],
    det: dict[str, dict[str, Any]],
    truth: Mapping[str, GpsJamCell] | None,
    args: argparse.Namespace,
    grades: Mapping[str, str],
    coverage: bool = False,
) -> Run:
    res = replay(
        obs,
        settings,
        aoi.name,
        day[0],
        day[1],
        step_min=args.step_min,
        detectors=det,
        playbook={},
        source_grades=grades,
        collect_coverage=coverage,
    )
    sc = None
    if truth is not None:
        observed = _observed_cells(obs, det)
        preds = daily_predictions(res.jamming_evidence(), res.jamming_alerts(), observed)
        sc = score(truth, preds, aoi, min_aircraft=args.gpsjam_min_aircraft)
    j = det.get("jamming", {})
    return Run(label, float(j.get("ratio_alert", 0.1)), int(j.get("min_aircraft", 5)), res, sc)


def _observed_cells(obs: Sequence[Any], det: Mapping[str, dict[str, Any]]) -> set[str]:
    """r4 cells with >= 1 airborne true-ADS-B report carrying NIC or NACp (jamming quality)."""
    res = int(det.get("jamming", {}).get("h3_res", 4))
    out = set()
    for o in obs:
        if o.on_ground or not o.is_true_adsb or (o.nic is None and o.nac_p is None):
            continue
        out.add(o.h3_r4 if res == 4 and o.h3_r4 else cell_of(o, res))
    return out


def _rank(r: Run) -> tuple[float, ...]:
    assert r.score is not None
    s = r.score
    return (
        round(s.red.f1 or 0.0, 6),
        round(s.red.precision or 0.0, 6),
        round(s.yellow.f1 or 0.0, 6),
        r.ratio_alert,  # ties -> the more conservative threshold
        float(r.min_aircraft),
    )


def recommend(current: Run, sweep: Sequence[Run]) -> list[str]:
    """Deterministic recommendation text (proposal only)."""
    scored = [r for r in sweep if r.score is not None]
    if not scored or current.score is None:
        return ["No GPSJam truth supplied: no threshold recommendation (coverage only)."]
    if all(r.score.red.f1 is None and r.score.yellow.f1 is None for r in scored if r.score):
        return [
            "Insufficient truth: no GPSJam red/yellow cells were matched by any alert in the "
            "sweep (F1 undefined everywhere). **Keep the current values**; rerun on a day with "
            "known interference."
        ]
    best = max(scored, key=_rank)
    cur = current.score
    b = best.score
    assert b is not None
    lines = [
        f"Best sweep point by (F1 red, precision red, F1 yellow+; ties -> higher thresholds): "
        f"**ratio_alert = {best.ratio_alert}, min_aircraft = {best.min_aircraft}** "
        f"(F1 red {_fmt(b.red.f1)}, precision {_fmt(b.red.precision)}, recall "
        f"{_fmt(b.red.recall)}; F1 yellow+ {_fmt(b.yellow.f1)}).",
        f"Current config (ratio_alert = {current.ratio_alert}, min_aircraft = "
        f"{current.min_aircraft}): F1 red {_fmt(cur.red.f1)}, precision "
        f"{_fmt(cur.red.precision)}, recall {_fmt(cur.red.recall)}; F1 yellow+ "
        f"{_fmt(cur.yellow.f1)}.",
    ]
    if _rank(best)[:3] <= _rank(current)[:3]:
        lines.append(
            "**Proposal: keep the current values** — no sweep point beats them on this day."
        )
    else:
        lines.append(
            f"**Proposal: ratio_alert {current.ratio_alert} -> {best.ratio_alert}, min_aircraft "
            f"{current.min_aircraft} -> {best.min_aircraft}** (ratio_clear scaled to "
            f"{_variant({'jamming': {}}, ratio_alert=best.ratio_alert)['jamming']['ratio_clear']}"
            f" to keep the hysteresis band)."
        )
    lines.append(
        "Adopt only after the same result holds on >= 7 days including a known-quiet day; "
        "config changes need the lead's or user's approval (this report does not edit config)."
    )
    return lines


def _score_rows(s: ScoreReport) -> list[str]:
    out = [
        "| level | TP | FP | FN | TN | precision | recall | F1 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for b in (s.red, s.yellow):
        out.append(
            f"| {b.level} | {b.tp} | {b.fp} | {b.fn} | {b.tn} | {_fmt(b.precision)} | "
            f"{_fmt(b.recall)} | {_fmt(b.f1)} |"
        )
    return out


def _confusion(s: ScoreReport) -> list[str]:
    out = [
        "| GPSJam \\ Heimdall | " + " | ".join(PREDS) + " |",
        "|---" * (len(PREDS) + 1) + "|",
    ]
    for lvl in [*LEVELS, "no_data"]:
        out.append(f"| {lvl} | " + " | ".join(str(s.confusion[lvl][p]) for p in PREDS) + " |")
    return out


def _bucket_table(res: ReplayResult, thresholds: Sequence[int]) -> list[str]:
    totals = [t for c in res.coverage.values() for t in c.totals]
    n = len(totals)
    out = ["| quality aircraft per cell-window | cell-windows | share |", "|---|---|---|"]
    for lo, hi in TOTAL_BUCKETS:
        k = sum(1 for t in totals if t >= lo and (hi is None or t <= hi))
        label = f"{lo}-{hi}" if hi is not None else f">= {lo}"
        out.append(f"| {label} | {k} | {_fmt(k / n if n else None, 2)} |")
    out.append("")
    for m in thresholds:
        k = sum(1 for t in totals if t >= m)
        out.append(
            f"- cell-windows with >= {m} quality aircraft: {k}/{n} "
            f"({_fmt(k / n if n else None, 2)})"
        )
    return out


def _lead_rows(leads: Sequence[LeadTime]) -> list[str]:
    out = ["| reference (UTC) | first alert (UTC) | lead (min) | alert |", "|---|---|---|---|"]
    for lt in leads:
        fa = lt.first_alert.strftime("%H:%M") if lt.first_alert else "missed"
        lead = _fmt(lt.lead_s / 60 if lt.lead_s is not None else None, 1)
        out.append(f"| {lt.ref.isoformat()} | {fa} | {lead} | {(lt.alert_id or '—')[:10]} |")
    return out


def render_report(
    *,
    aoi: AOI,
    date: str,
    args: argparse.Namespace,
    n_files: int,
    n_obs: int,
    gpsjam_sha: str | None,
    effective: Mapping[str, dict[str, Any]],
    current: Run,
    sweep: Sequence[Run],
    sparse: Run,
    truth: Mapping[str, GpsJamCell] | None,
    leads: Sequence[LeadTime] | None,
) -> str:
    j = effective.get("jamming", {})
    res = current.result
    L: list[str] = [f"# Backtest — {aoi.name} — {date}", ""]
    if args.note:
        L += [f"> {args.note}", ""]
    L += [
        "Generated by `python -m heimdall.backtest` (deterministic; no wall-clock data). "
        "Threshold changes below are **proposals only** — config/config.yaml is unchanged.",
        "",
        "## Inputs",
        "",
        f"- AOI `{aoi.name}` ({aoi.role}): centre {aoi.lat}, {aoi.lon}, radius {aoi.radius_nm} NM",
        f"- Trace files: {n_files} (provider `{args.provider}`), observations in AOI: {n_obs}",
        f"- GPSJam day file: {'sha256 ' + gpsjam_sha if gpsjam_sha else 'none'}"
        + (f", {sum(1 for _ in truth)} cells" if truth is not None else ""),
        f"- Replay: {res.ticks} ticks of {args.step_min:g} min, {date}T00:00Z-24:00Z; detectors "
        f"`{jamming.VERSION}`, `{spoofing.VERSION}`, `{orbit.VERSION}`",
        "- Effective jamming config: "
        + ", ".join(
            f"{k}={j.get(k, jamming.DEFAULTS[k])}"
            for k in (
                "window_min",
                "ratio_alert",
                "ratio_clear",
                "min_aircraft",
                "robust_z",
                "mad_floor",
                "baseline_min_samples",
                "confidence_cap",
            )
        ),
        "",
        "## Results — current config",
        "",
        f"- Alerts: jamming {current.n_alerts}, spoofing "
        f"{sum(1 for a in res.alerts if a.kind == 'gnss_spoofing')}, orbit "
        f"{sum(1 for a in res.alerts if a.kind == 'mil_orbit')}, composite "
        f"{sum(1 for a in res.alerts if a.kind == 'composite')}",
        f"- Jamming Evidence: {len(res.jamming_evidence())} window-cells in "
        f"{len({e.h3_cell for e in res.jamming_evidence()})} cells",
        "",
    ]
    if current.score is not None:
        s = current.score
        L += ["### Precision / recall vs GPSJam", "", *_score_rows(s), ""]
        L += ["### Confusion table (r4 cells in AOI)", "", *_confusion(s), ""]
        L += [
            "### Cells of interest",
            "",
            "| cell | GPSJam good/bad | GPSJam R' | GPSJam level | Heimdall | Evidence | max R' |",
            "|---|---|---|---|---|---|---|",
        ]
        preds = daily_predictions(res.jamming_evidence(), res.jamming_alerts(), [])
        for cell, (lvl, pred) in s.cells.items():
            if lvl not in ("red", "yellow") and pred not in ("alert", "evidence_only"):
                continue
            t = truth.get(cell) if truth else None
            p = preds.get(cell)
            L.append(
                f"| {cell} | {f'{t.good}/{t.bad}' if t else '—'} | "
                f"{_fmt(t.ratio if t else None)} | {lvl} | {pred} | "
                f"{p.n_evidence if p else 0} | {_fmt(p.max_ratio if p else None)} |"
            )
        L.append("")
    L += [
        "## Threshold sweep (jamming)",
        "",
        "ratio_clear is scaled with ratio_alert (same proportion as configured). "
        "F1 = — when undefined (no positives predicted or no positive truth).",
        "",
        "| ratio_alert | min_aircraft | jam alerts | TP/FP/FN red | P red | R red | F1 red "
        "| P yellow+ | R yellow+ | F1 yellow+ |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sweep:
        if r.score is None:
            L.append(
                f"| {r.ratio_alert} | {r.min_aircraft} | {r.n_alerts} | — | — | — | — | — | — | — |"
            )
            continue
        red, yel = r.score.red, r.score.yellow
        mark = " (current)" if r.label == "current" else ""
        L.append(
            f"| {r.ratio_alert}{mark} | {r.min_aircraft} | {r.n_alerts} | "
            f"{red.tp}/{red.fp}/{red.fn} | {_fmt(red.precision)} | {_fmt(red.recall)} | "
            f"{_fmt(red.f1)} | {_fmt(yel.precision)} | {_fmt(yel.recall)} | {_fmt(yel.f1)} |"
        )
    L += ["", "## Recommendation", "", *recommend(current, sweep), ""]
    L += [
        "## Coverage and sparse-region proposal (SE Asia AOIs)",
        "",
        "Quality aircraft (true ADS-B with NIC/NACp) per r4 cell per "
        f"{j.get('window_min', 30)}-min window, over windows where the cell had any:",
        "",
        *_bucket_table(res, sorted({3, int(j.get("min_aircraft", 5)), 8})),
        "",
        "The Myanmar snapshots (tests/fixtures/adsb_lol: 3, adsb_fi: 10 aircraft in a 250 NM "
        "radius) show that at the global `min_aircraft = 5` almost no SE Asia cell-window is "
        "judgeable. Proposed per-AOI override for `myanmar`, `south_china_sea`, `malacca_sg` "
        "(and `taiwan_luzon` if its coverage table looks like this one):",
        "",
        "```yaml",
        "detectors: {jamming: {" + ", ".join(f"{k}: {v}" for k, v in SPARSE_PROFILE.items()) + "}}",
        "```",
        "",
        "- `window_min: 60` — an airliner crosses an r4 cell in ~4-6 min, so the number of "
        "distinct aircraft per cell-window grows ~linearly with the window; doubling it roughly "
        "doubles the judgeable cell-windows at the cost of ~30 min extra detection latency.",
        "- `min_aircraft: 3` — the (bad - 1)/total de-noise still needs >= 2 independent "
        "degraded aircraft (R' >= 1/3 at total 3), so one faulty transponder can never fire.",
        "- `confidence_cap: 0.5` — severity = score x confidence <= 0.5 < `severity.high` (0.6): "
        "thin-coverage alerts top out at MEDIUM and carry the gap "
        '"sparse-traffic profile: confidence capped".',
        "- Persistence (>= 2 steps of anomaly span) and hysteresis are unchanged.",
        "",
    ]
    if sparse.score is not None:
        red, yel = sparse.score.red, sparse.score.yellow
        L += [
            "Sparse profile replayed on this day's data (for comparison only; the control AOI "
            "keeps the global config):",
            "",
            "| profile | jam alerts | TP/FP/FN red | P red | R red | F1 red | F1 yellow+ |",
            "|---|---|---|---|---|---|---|",
            f"| sparse | {sparse.n_alerts} | {red.tp}/{red.fp}/{red.fn} | "
            f"{_fmt(red.precision)} | {_fmt(red.recall)} | {_fmt(red.f1)} | {_fmt(yel.f1)} |",
            "",
        ]
    if leads is not None:
        L += ["## Lead time vs reference timestamps", "", *_lead_rows(leads), ""]
    L += [
        "## Limitations",
        "",
        f"- One day: the robust-z baseline needs {j.get('baseline_min_samples', 288)} samples "
        "per cell, so the z-test is skipped (gap 'no baseline') and only the absolute ratio "
        "trigger is calibrated here.",
        "- GPSJam aggregates 24 h per cell; Heimdall alerts on 30-min windows. A cell counts as "
        "an alert if any jamming alert opened there during the day; exact r4 cell match only "
        "(no neighbour tolerance).",
        "- GPSJam CSV column layout is assumed (hex, count_good_aircraft, count_bad_aircraft) "
        "pending task 005 source verification.",
        "- Trace rows without a readsb detail object have unknown NIC/NACp and are ignored by "
        "the jamming ratio (no forward fill).",
        "",
    ]
    return "\n".join(L)


def _read_refs(path: str) -> list[datetime]:
    out = []
    for line in Path(path).read_text().splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        out.append(t if t.tzinfo else t.replace(tzinfo=UTC))
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m heimdall.backtest", description=__doc__)
    p.add_argument("--aoi", required=True, help="AOI name from config/config.yaml")
    p.add_argument("--date", required=True, help="UTC day to replay, YYYY-MM-DD")
    p.add_argument("--traces", help="dir/file of readsb trace JSON (default data/raw/traces/DATE)")
    p.add_argument("--gpsjam", help="GPSJam day CSV (hex,count_good_aircraft,count_bad_aircraft)")
    p.add_argument("--refs", help="text file of reference ISO timestamps for lead time")
    p.add_argument("--out-dir", default="reports")
    p.add_argument("--step-min", type=float, default=5.0)
    p.add_argument("--gpsjam-min-aircraft", type=int, default=1, help="gray below this count")
    p.add_argument("--provider", default=PROVIDER, help="provider name for replayed obs")
    p.add_argument("--note", default="", help="banner line for the report (e.g. SYNTHETIC)")
    p.add_argument("--no-sweep", action="store_true", help="skip the threshold sweep")
    return p


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> Path:
    args = build_parser().parse_args(argv)
    settings = settings or config.settings()
    try:
        aoi = settings.aoi(args.aoi)
    except StopIteration:
        raise SystemExit(f"unknown AOI {args.aoi!r}") from None
    day0 = datetime.fromisoformat(args.date).replace(tzinfo=UTC)
    day = (day0, day0 + timedelta(days=1))
    traces = Path(args.traces or f"data/raw/traces/{args.date}")
    files = trace_files(traces)
    if not files:
        raise SystemExit(f"no trace files under {traces}")
    effective = settings.detectors_for(aoi)
    lookback = max(
        float(effective.get(k, {}).get("window_min", d))
        for k, d in (("jamming", 30), ("spoofing", 30), ("orbit", 75))
    )
    obs = list(
        iter_trace_dir(
            traces,
            args.provider,
            start=day[0] - timedelta(minutes=lookback + args.step_min),
            end=day[1],
            area=(aoi.lat, aoi.lon, aoi.radius_nm),
        )
    )
    truth = load_gpsjam(args.gpsjam) if args.gpsjam else None
    grades = grades_from_sources(config.sources())
    common = (obs, settings, aoi, day)
    current = _run("current", *common, effective, truth, args, grades, coverage=True)
    sweep: list[Run] = []
    if not args.no_sweep:
        j = effective.get("jamming", {})
        cur_key = (float(j.get("ratio_alert", 0.1)), int(j.get("min_aircraft", 5)))
        for r in RATIO_ALERTS:
            for m in MIN_AIRCRAFT:
                if (r, m) == cur_key:
                    sweep.append(current)
                    continue
                det = _variant(effective, ratio_alert=r, min_aircraft=m)
                sweep.append(_run(f"{r}/{m}", *common, det, truth, args, grades))
    sparse = _run("sparse", *common, _variant(effective, **SPARSE_PROFILE), truth, args, grades)
    leads = (
        lead_times(current.result.alerts, _read_refs(args.refs), raised_at=current.result.raised_at)
        if args.refs
        else None
    )
    text = render_report(
        aoi=aoi,
        date=args.date,
        args=args,
        n_files=len(files),
        n_obs=current.result.n_obs,
        gpsjam_sha=_sha(Path(args.gpsjam)) if args.gpsjam else None,
        effective=effective,
        current=current,
        sweep=sweep,
        sparse=sparse,
        truth=truth,
        leads=leads,
    )
    out = Path(args.out_dir) / f"backtest-{aoi.name}-{args.date}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(out)
    return out
