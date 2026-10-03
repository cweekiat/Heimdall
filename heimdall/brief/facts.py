"""Shift fact sheet and trends (task 044). Pure: alerts in, a small structured summary out.

The fact sheet is what a local language model may narrate, and what the UI always shows as
"Trends" without any model. It contains only Heimdall's own fields (kind, area, severity, status,
times, refs, bands, rule descriptions it wrote itself): never source free text (notice text, ship
names), so nothing scraped can act as an instruction to the model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from heimdall.schemas import Alert, AlertStatus

OPEN = (AlertStatus.NEW, AlertStatus.ACKNOWLEDGED, AlertStatus.UNDER_REVIEW, AlertStatus.SHELVED)
KIND_WORDS = {
    "gnss_jamming": "GPS jamming",
    "gnss_spoofing": "GPS spoofing",
    "ais_spoofing": "AIS position spoofing",
    "ais_gap": "ships going dark",
    "mil_orbit": "military orbits",
    "mil_deployment": "military aircraft flowing in",
    "mil_surge": "military activity above normal",
    "mil_high_interest": "high-interest military aircraft",
    "mil_buildup": "possible military build-up",
    "thermal": "fire / heat detections",
    "spectrum_anomaly": "radio band far from normal",
    "warning_area": "warning areas",
    "composite": "several signs in one place",
}
BAND_WORDS = {
    "hf": "HF 3-30 MHz",
    "vhf_marine": "VHF marine / AIS",
    "l_1090": "1090 MHz ADS-B",
    "l1_gnss": "GPS L1",
    "l_1400": "L-band 1.4 GHz",
    "sat_downlink": "satellite downlinks",
    "c_sar": "C-band SAR",
}


def _trend(now_n: int, prev_n: int) -> str:
    if prev_n == 0:
        return "new" if now_n else "none"
    r = now_n / prev_n
    return "up" if r >= 1.5 else "down" if r <= 0.67 else "steady"


def build_facts(
    alerts: Sequence[Alert],
    now: datetime,
    hours: float,
    band_states: Sequence[Mapping[str, Any]] = (),
    max_items: int = 12,
    area_labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Facts for the last `hours`, compared with the `hours` before that. `area_labels` maps area
    ids to the names analysts know (drawn areas have generated ids)."""
    labels = dict(area_labels or {})
    alerts = [
        a.model_copy(update={"aoi": labels.get(a.aoi, a.aoi)}) if a.aoi else a for a in alerts
    ]
    t0 = now - timedelta(hours=hours)
    tp = t0 - timedelta(hours=hours)
    cur = [a for a in alerts if a.last_seen >= t0 and a.first_seen <= now]
    started = [a for a in cur if a.first_seen >= t0]
    prev = [a for a in alerts if tp <= a.first_seen < t0]

    def by(xs: Sequence[Alert], key: str) -> Counter[str]:
        return Counter(
            str(getattr(a, key).value if key == "kind" else getattr(a, key) or "-") for a in xs
        )

    k_now, k_prev = by(started, "kind"), by(prev, "kind")
    a_now, a_prev = by(started, "aoi"), by(prev, "aoi")
    trends_kind = [
        {
            "kind": k,
            "what": KIND_WORDS.get(k, k),
            "now": k_now[k],
            "before": k_prev[k],
            "trend": _trend(k_now[k], k_prev[k]),
        }
        for k in sorted(set(k_now) | set(k_prev), key=lambda k: -k_now[k])
    ]
    trends_area = [
        {"area": ar, "now": a_now[ar], "before": a_prev[ar], "trend": _trend(a_now[ar], a_prev[ar])}
        for ar in sorted(set(a_now) | set(a_prev), key=lambda x: -a_now[x])
    ]
    bands = Counter(b.value for a in started for b in a.bands if b.value != "none")
    status = Counter(a.status.value for a in cur)
    sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    top = sorted(
        cur,
        key=lambda a: (-sev_rank.get(a.severity.value, 0), -a.confidence, a.first_seen),
    )[:max_items]
    items = [
        {
            "ref": a.ref or a.id[:10],
            "what": KIND_WORDS.get(a.kind.value, a.kind.value),
            "area": a.aoi,
            "severity": a.severity.value,
            "status": a.status.value,
            "first_seen": a.first_seen.strftime("%Y-%m-%d %H:%M UTC"),
            "last_seen": a.last_seen.strftime("%Y-%m-%d %H:%M UTC"),
            "still_active": now - a.last_seen <= timedelta(hours=1),
            "bands": [BAND_WORDS.get(b.value, b.value) for b in a.bands if b.value != "none"],
            "confidence_pct": round(a.confidence * 100),
            "why": a.summary[:300],
        }
        for a in top
    ]
    anomalous = [
        {
            "area": s.get("area"),
            "band": BAND_WORDS.get(str(s.get("band")), s.get("band")),
            "state": s.get("state"),
        }
        for s in band_states
        if s.get("state") in ("elevated", "anomalous")
    ][:max_items]
    return {
        "period": {
            "from": t0.strftime("%Y-%m-%d %H:%M UTC"),
            "to": now.strftime("%Y-%m-%d %H:%M UTC"),
            "hours": hours,
        },
        "counts": {
            "alerts_in_period": len(cur),
            "started_in_period": len(started),
            "started_previous_period": len(prev),
            "needs_action": status.get("new", 0),
            "in_progress": sum(status.get(s.value, 0) for s in OPEN if s != AlertStatus.NEW),
            "confirmed": status.get("confirmed", 0),
            "closed_false_alarm": status.get("dismissed", 0),
            "closed_resolved": status.get("resolved", 0),
            "ended_on_their_own": status.get("expired", 0),
        },
        "trend_by_type": trends_kind,
        "trend_by_area": trends_area,
        "bands": [{"band": BAND_WORDS.get(b, b), "alerts": n} for b, n in bands.most_common()],
        "spectrum_board": anomalous,
        "top_alerts": items,
        "refs": [i["ref"] for i in items],
    }


def trend_lines(f: Mapping[str, Any]) -> list[str]:
    """Deterministic, plain-language trend statements (shown with or without a model)."""
    c = f["counts"]
    out = [
        f"{c['started_in_period']} alerts started in the last {f['period']['hours']:g} h "
        f"(previous {f['period']['hours']:g} h: {c['started_previous_period']}). "
        f"{c['needs_action']} still need action, {c['in_progress']} in progress, "
        f"{c['confirmed']} confirmed real."
    ]
    for t in f["trend_by_type"][:6]:
        if t["trend"] in ("up", "new"):
            out.append(f"More {t['what']}: {t['now']} (before {t['before']}).")
        elif t["trend"] == "down":
            out.append(f"Less {t['what']}: {t['now']} (before {t['before']}).")
    for t in f["trend_by_area"][:4]:
        if t["trend"] == "new" and t["now"]:
            out.append(f"New activity in {t['area']}: {t['now']} alerts (none before).")
    if f["bands"]:
        out.append(
            "Bands affected: " + ", ".join(f"{b['band']} ({b['alerts']})" for b in f["bands"]) + "."
        )
    for s in f["spectrum_board"][:4]:
        out.append(f"Spectrum board: {s['band']} {s['state']} over {s['area']}.")
    if c["closed_false_alarm"]:
        out.append(f"{c['closed_false_alarm']} alerts closed as false alarms.")
    return out
