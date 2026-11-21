"""Feedback-driven tuning (P7): how often each detector is right, and threshold
changes the analysts' own verdicts support. Read-only: it PROPOSES; nothing here edits config.

Verdicts: an alert closed as `confirmed` or `resolved` is real; `dismissed` is a false alarm (its
reason comes from the feedback record or the status note). Everything else is unreviewed and is
never counted as either. Each reviewed alert counts for every detector in its evidence.

Precision = real / reviewed, with a Wilson 95 % interval (small samples stay honest). Reviews
are not a random sample (analysts review what looks interesting), stated in the report.

Threshold proposals (what-if on the reviewed alerts only), per tunable rule in TUNABLE:
* the alert's value for the rule = the most extreme value over its evidence for that detector
  (the alert would still fire if any evidence passed);
* the proposed threshold is the tightest value that still keeps EVERY confirmed alert;
* proposed only with >= MIN_FALSE false alarms and >= MIN_REAL confirmed alerts for that
  detector, when it would remove >= MIN_GAIN of the false alarms; only rule results judged
  against the CURRENT config value count (so changed, hysteresis or per-mode thresholds, e.g.
  CYGNSS land vs ocean, jamming's clear ratio, are never mixed up).
Removing an alert in the what-if ignores fusion and persistence: an estimate, stated as such.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from heimdall.ports import Store
from heimdall.schemas import Alert, AlertStatus, Evidence

MIN_FALSE = 5
MIN_REAL = 3
MIN_GAIN = 0.3
REAL = {AlertStatus.CONFIRMED, AlertStatus.RESOLVED}
FALSE = {AlertStatus.DISMISSED}


@dataclass(frozen=True)
class Knob:
    """A rule whose threshold is one config value. `sign`: the rule compares value against
    sign x config (HF z rules store -z_min). `tighter`: +1 when raising the config value makes
    the rule stricter, -1 when lowering it does. `digits`: rounding of the proposal."""

    key: str
    tighter: int
    sign: float = 1.0
    digits: int = 2
    integer: bool = False


TUNABLE: dict[str, dict[str, Knob]] = {
    "jamming": {
        "min_aircraft": Knob("min_aircraft", +1, integer=True),
        "min_degraded": Knob("min_degraded", +1, integer=True),
        "robust_z": Knob("robust_z", +1, digits=1),
        "degraded_ratio": Knob("ratio_alert", +1),
    },
    "spoofing": {
        "signals": Knob("min_signals", +1, integer=True),
        "S2_alt_divergence": Knob("alt_divergence_ft", +1, digits=0),
    },
    "orbit": {
        "min_points": Knob("min_points", +1, integer=True),
        "circ_var": Knob("circ_var_min", +1),
        "cum_turn": Knob("cum_turn_min_deg", +1, digits=0),
        "displacement": Knob("displacement_max", -1),
    },
    "ais_spoofing": {
        "A1_speed": Knob("teleport_kn", +1, digits=0),
        "A3_ring_vessels": Knob("ring_min_vessels", +1, integer=True),
        "A3_ring_cv": Knob("ring_cv_max", -1, digits=3),
        "A4_radius": Knob("circle_max_radius_km", -1, digits=1),
        "A4_turn": Knob("circle_min_turn_deg", +1, digits=0),
    },
    "ais_gap": {
        "gap_h": Knob("gap_min_h", +1, digits=1),
        "reception_coverage": Knob("gap_covered_frac", +1),
    },
    "cygnss": {
        "C1_noise_level": Knob("level_db", +1, digits=1),
        "C2_change_vs_own_baseline": Knob("change_db", +1, digits=1),
    },
    "hf_path": {
        "hf_count_drop": Knob("z_min", +1, sign=-1.0, digits=1),
        "hf_snr_drop": Knob("z_min", +1, sign=-1.0, digits=1),
    },
    "hf_rx": {
        "hf_rx_snr_drop": Knob("z_min", +1, sign=-1.0, digits=1),
        "hf_rx_decode_drop": Knob("z_min", +1, sign=-1.0, digits=1),
    },
}
# detector name -> config section under `detectors:`
SECTION = {"hf_path": "hf"}


@dataclass
class Review:
    alert_id: str
    kind: str
    aoi: str | None
    verdict: str | None  # "real" | "false" | None (unreviewed)
    reason: str | None
    evidence: dict[str, list[Evidence]] = field(default_factory=dict)  # detector -> evidence


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


def _round(v: float, k: Knob) -> float:
    """Round towards the looser side, so the rounded threshold still keeps the edge value."""
    q = 1 if k.integer else 10**k.digits
    return float(math.floor(v * q + 1e-9) if k.tighter > 0 else math.ceil(v * q - 1e-9)) / q


def _alert_value(evs: Sequence[Evidence], rule: str, k: Knob, cur: float) -> float | None:
    """Value of `rule` for the alert, in config units (sign applied): the one most likely to
    pass over its evidence. Only rule results judged against the current config value count
    (hysteresis or per-mode thresholds, e.g. jamming's clear ratio, are other knobs)."""
    vals = [
        r.value * k.sign
        for e in evs
        for r in e.rules
        if r.rule == rule
        and isinstance(r.value, int | float)
        and not isinstance(r.value, bool)
        and isinstance(r.threshold, int | float)
        and math.isclose(float(r.threshold) * k.sign, cur, rel_tol=1e-6, abs_tol=1e-9)
    ]
    if not vals:
        return None
    return max(vals) if k.tighter > 0 else min(vals)


def propose(
    det: str, reviews: Sequence[Review], current: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """(proposals, notes) for one detector from its reviewed alerts."""
    real = [r for r in reviews if r.verdict == "real"]
    false = [r for r in reviews if r.verdict == "false"]
    notes: list[str] = []
    if len(false) < MIN_FALSE or len(real) < MIN_REAL:
        notes.append(
            f"not enough reviews to propose thresholds yet: {len(false)}/{MIN_FALSE} false "
            f"alarms and {len(real)}/{MIN_REAL} confirmed needed"
        )
        return [], notes
    out: list[dict[str, Any]] = []
    for rule, k in TUNABLE.get(det, {}).items():
        cur = current.get(k.key)
        if not isinstance(cur, int | float) or isinstance(cur, bool):
            continue
        c = float(cur)
        rv = [v for r in real if (v := _alert_value(r.evidence[det], rule, k, c)) is not None]
        fv = [v for r in false if (v := _alert_value(r.evidence[det], rule, k, c)) is not None]
        if len(rv) < MIN_REAL or len(fv) < MIN_FALSE:
            if not rv and not fv:
                notes.append(
                    f"{rule}: no reviewed alert was judged at the current {k.key} = {cur} "
                    "(changed since, or not used): skipped"
                )
            continue
        # tightest value keeping every confirmed alert
        edge = min(rv) if k.tighter > 0 else max(rv)
        new = _round(edge, k)
        if k.tighter > 0:
            if new <= c:
                continue
            removed = [v for v in fv if v < new]
        else:
            if new >= c:
                continue
            removed = [v for v in fv if v > new]
        gain = len(removed) / len(fv)
        if gain < MIN_GAIN:
            continue
        section = SECTION.get(det, det)
        out.append(
            {
                "detector": det,
                "rule": rule,
                "config": f"detectors.{section}.{k.key}",
                "current": cur,
                "proposed": int(new) if k.integer else new,
                "false_removed": len(removed),
                "false_total": len(fv),
                "real_kept": len(rv),
                "why": f"{len(removed)} of {len(fv)} reviewed false alarms had {rule} "
                f"{'below' if k.tighter > 0 else 'above'} {new:g}; every one of {len(rv)} "
                "confirmed alerts was beyond it",
                "yaml": f"{k.key}: {int(new) if k.integer else new:g}  # 061 feedback: "
                f"{len(removed)}/{len(fv)} false alarms removed, {len(rv)}/{len(rv)} confirmed "
                "kept (what-if on reviewed alerts)",
            }
        )
    out.sort(key=lambda p: -int(p["false_removed"]))
    return out, notes


REASON_HINTS = {
    "Equipment fault on one aircraft or ship": "single-platform faults: consider a higher "
    "minimum of distinct aircraft or ships",
    "Data glitch or feed problem": "feed problems: check the source's health history at those "
    "times; tighter coverage checks rather than thresholds",
    "Known exercise, test or notice": "announced activity is real, not a detector error: link "
    "the notice instead of tuning the threshold",
    "Duplicate of another alert": "duplicates: fusion merge distance or time, not detector "
    "thresholds",
}


def report(
    reviews: Sequence[Review], detectors_cfg: Mapping[str, Any], *, days: float, now: datetime
) -> dict[str, Any]:
    by_det: dict[str, list[Review]] = defaultdict(list)
    for r in reviews:
        for det in r.evidence:
            by_det[det].append(r)
    rows: list[dict[str, Any]] = []
    for det, rs in sorted(by_det.items()):
        n_real = sum(r.verdict == "real" for r in rs)
        n_false = sum(r.verdict == "false" for r in rs)
        reviewed = n_real + n_false
        reasons = Counter((r.reason or "no reason given") for r in rs if r.verdict == "false")
        by_aoi: dict[str, Counter[str]] = defaultdict(Counter)
        for r in rs:
            by_aoi[r.aoi or "-"][r.verdict or "unreviewed"] += 1
        cur = detectors_cfg.get(SECTION.get(det, det)) or {}
        props, notes = propose(det, [r for r in rs if r.verdict], cur)
        hints = [REASON_HINTS[k] for k in reasons if k in REASON_HINTS]
        rows.append(
            {
                "detector": det,
                "alerts": len(rs),
                "reviewed": reviewed,
                "real": n_real,
                "false": n_false,
                "unreviewed": len(rs) - reviewed,
                "precision": round(n_real / reviewed, 3) if reviewed else None,
                "precision_ci95": wilson(n_real, reviewed),
                "reasons": dict(reasons.most_common()),
                "areas": [
                    {"aoi": a, **dict(c)}
                    for a, c in sorted(by_aoi.items(), key=lambda kv: -sum(kv[1].values()))
                ][:12],
                "proposals": props,
                "notes": notes,
                "hints": hints,
                "tunable": sorted(TUNABLE.get(det, {})),
            }
        )
    rows.sort(key=lambda r: (-int(r["reviewed"]), -int(r["alerts"])))
    return {
        "generated": now.isoformat(),
        "days": days,
        "alerts": len(reviews),
        "reviewed": sum(1 for r in reviews if r.verdict),
        "detectors": rows,
        "rules": {
            "min_false": MIN_FALSE,
            "min_real": MIN_REAL,
            "min_gain": MIN_GAIN,
        },
        "limits": "Reviews are not a random sample: analysts review what looks interesting. The "
        "what-if ignores fusion and persistence, so the real effect of a change can differ. "
        "Proposals are never applied automatically: change config/config.yaml (with the "
        "provenance comment shown) and restart.",
    }


def collect(store: Store, since: datetime, limit: int = 20_000) -> list[Review]:
    """Alerts first seen since `since`, with their verdict, false-alarm reason and evidence."""
    alerts: list[Alert] = store.list_alerts(since=since, limit=limit)
    ev_ids = sorted({i for a in alerts for i in a.evidence_ids})
    evs: dict[str, Evidence] = {}
    for i in range(0, len(ev_ids), 500):
        for e in store.get_evidence(ev_ids[i : i + 500]):
            evs[e.id] = e
    out = []
    for a in alerts:
        verdict = "real" if a.status in REAL else "false" if a.status in FALSE else None
        reason = None
        if verdict == "false":
            fb = [f for f in store.feedback_for(a.id) if f.verdict == "dismiss" and f.reason]
            if fb:
                reason = max(fb, key=lambda f: f.ts).reason
            else:
                ns = [
                    n
                    for n in store.notes_for(a.id)
                    if n.status_to == AlertStatus.DISMISSED and n.text
                ]
                reason = max(ns, key=lambda n: n.ts).text if ns else None
        by: dict[str, list[Evidence]] = defaultdict(list)
        for eid in a.evidence_ids:
            if eid in evs:
                by[evs[eid].detector].append(evs[eid])
        out.append(Review(a.id, a.kind.value, a.aoi, verdict, reason, dict(by)))
    return out


def build(
    store: Store, detectors_cfg: Mapping[str, Any], now: datetime, days: float
) -> dict[str, Any]:
    return report(collect(store, now - timedelta(days=days)), detectors_cfg, days=days, now=now)


def markdown(r: Mapping[str, Any]) -> str:
    """Plain report for the terminal or a Markdown file."""
    lines = [
        f"# Detector tuning report ({r['generated'][:16]} UTC, last {r['days']:g} days)",
        "",
        f"{r['alerts']} alerts, {r['reviewed']} reviewed (confirmed or false alarm).",
        "",
        "| Detector | Alerts | Reviewed | Real | False | Precision (95 % CI) |",
        "|---|---|---|---|---|---|",
    ]
    for d in r["detectors"]:
        ci = d["precision_ci95"]
        p = "-" if d["precision"] is None else f"{d['precision']:.0%} ({ci[0]:.0%}-{ci[1]:.0%})"
        lines.append(
            f"| {d['detector']} | {d['alerts']} | {d['reviewed']} | {d['real']} | {d['false']} "
            f"| {p} |"
        )
    for d in r["detectors"]:
        if not (d["proposals"] or d["reasons"]):
            continue
        lines += ["", f"## {d['detector']}"]
        if d["reasons"]:
            lines.append(
                "False-alarm reasons: " + ", ".join(f"{k} ({v})" for k, v in d["reasons"].items())
            )
        lines += [
            f"- Proposal `{p['config']}`: {p['current']} -> {p['proposed']}. {p['why']}."
            for p in d["proposals"]
        ]
        lines += [f"- {h}" for h in d["hints"]]
        lines += [f"- Note: {n}" for n in d["notes"]]
    lines += ["", r["limits"]]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    from heimdall import config
    from heimdall.adapters.duckdb_store import DuckDBStore

    ap = argparse.ArgumentParser(description="Detector tuning report from analyst verdicts")
    ap.add_argument("--store", default="data/heimdall.duckdb")
    ap.add_argument("--days", type=float, default=30)
    a = ap.parse_args()
    now = datetime.now(UTC)
    print(markdown(build(DuckDBStore(a.store), config.settings().detectors, now, a.days)))
