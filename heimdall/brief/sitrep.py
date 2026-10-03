"""Daily SITREP (P7, task 053): one Markdown situation report for the last 24 h. Pure.

Built only from Heimdall's own facts: the shift fact sheet (`facts.build_facts`), the theatre
activity index (051), the military summary (039) and natural factors (space weather). Headlines
and other scraped text are never included. Times are UTC. A local model may narrate the same fact
sheet separately (044); the SITREP itself is deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from heimdall.brief.facts import trend_lines


def _md_cell(v: object) -> str:
    return str(v if v is not None else "").replace("|", "/").replace("\n", " ")[:120]


def build_sitrep(
    facts: Mapping[str, Any],
    theatres: Sequence[Mapping[str, Any]],
    military: Mapping[str, Any] | None,
    space: Mapping[str, Any],
    now: datetime,
) -> str:
    c = facts["counts"]
    p = facts["period"]
    out = [
        f"# Heimdall daily SITREP — {now:%d %b %Y} (UTC)",
        "",
        f"Period {p['from']} to {p['to']}. Generated {now:%Y-%m-%d %H:%M} UTC from Heimdall's own "
        "detections. Estimates from open data: absence of a signal is not absence of activity.",
        "",
        "## 1. Bottom line",
        *[f"- {t}" for t in trend_lines(facts)],
        "",
        "## 2. Theatres above their normal",
    ]
    hot = [t for t in theatres if t.get("level", 0) > 0]
    if not hot:
        out.append("- All theatres quiet (no component above its own normal).")
    for t in hot:
        above = [x for x in t.get("components", []) if x.get("above")]
        out.append(f"- **{t['name']}: {t['word']}** — " + "; ".join(x["why"] for x in above))
    out += ["", "## 3. Alerts that matter"]
    top = facts.get("top_alerts") or []
    if not top:
        out.append("- No alerts in the period.")
    else:
        out += [
            "| Ref | What | Area | Severity | Status | Bands | Last seen |",
            "|---|---|---|---|---|---|---|",
        ]
        for a in top:
            out.append(
                "| "
                + " | ".join(
                    _md_cell(x)
                    for x in (
                        a["ref"],
                        a["what"],
                        a["area"],
                        a["severity"],
                        a["status"] + ("" if a["still_active"] else " (ended)"),
                        ", ".join(a["bands"]) or "-",
                        a["last_seen"],
                    )
                )
                + " |"
            )
    out += [
        "",
        f"Workflow: {c['needs_action']} need action, {c['in_progress']} in progress, "
        f"{c['confirmed']} confirmed, {c['closed_resolved']} closed as ended, "
        f"{c['closed_false_alarm']} false alarms.",
        "",
        "## 4. Military air activity",
    ]
    if military:
        roles = ", ".join(f"{k} {v}" for k, v in list(military.get("roles", {}).items())[:8])
        out.append(
            f"- {military.get('contacts', 0)} military aircraft seen worldwide in the last "
            f"{military.get('window_min', 0)} min ({roles})."
        )
        for a in (military.get("high_interest") or [])[:10]:
            who = a.get("flight") or a.get("registration") or str(a.get("hex", "")).upper()
            out.append(
                f"- High interest: {_md_cell(who)} · "
                f"{_md_cell(a.get('type_name') or a.get('type'))}"
                f" · {_md_cell(a.get('operator') or a.get('country'))}"
            )
    else:
        out.append("- Military feed not available.")
    out += ["", "## 5. Natural factors"]
    kp, flare = space.get("kp_max"), space.get("flare")
    out.append(
        f"- Geomagnetic: Kp max {kp:g} in 24 h"
        + (" (storm: HF and GPS effects possible)" if kp >= 5 else "")
        if isinstance(kp, int | float)
        else "- Geomagnetic: no Kp data."
    )
    out.append(
        f"- Solar flares: strongest {flare} in 24 h."
        if flare
        else "- Solar flares: none above C class recorded."
    )
    out += [
        "",
        "## 6. Limits",
        "- Thresholds are priors unless a report says otherwise; an experimental band alone is "
        "capped at medium.",
        "- Indicators describe activity; they never predict a strike or name a target or an "
        "emitter type.",
        "",
    ]
    return "\n".join(out)
