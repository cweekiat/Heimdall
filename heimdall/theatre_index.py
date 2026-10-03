"""Theatre activity index (task 051): an explainable alternative to a single "DEFCON"-style score.

For each military theatre, a handful of components Heimdall measures itself, each compared with that
theatre's own normal and shown with its numbers. The level is only a count of components above
normal: 0 Quiet, 1 Watch, 2 Elevated, >= 3 High. It describes observed activity; it is not a threat
level, it predicts nothing and names no target. Pure: inputs are prepared by the API.

Components (priors; "learning" until a baseline exists):
* strategic military aircraft (tankers, bombers, surveillance, command posts, transport) over 6 h vs
  the theatre's hourly median (14 days): above when >= max(1.5 x median, median + 3)
* military indicator alerts open (deployment, surge, high interest, build-up): above when >= 1
* GPS interference alerts open in the theatre: above when >= 1
* radio band anomalies open in the theatre: above when >= 1
* conflict events (GDELT, 2+ sources) in 24 h vs the daily median of the 7 days before: above when
  >= max(2 x median, median + 3)
* new airspace / GPS / military notices in 24 h (shown; above when >= 3)
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

LEVELS = ("Quiet", "Watch", "Elevated", "High")
MIN_BASELINE_H = 72
MIN_CONFLICT_DAYS = 3


@dataclass
class Component:
    key: str
    label: str
    now: float
    normal: float | None
    above: bool
    why: str


@dataclass
class TheatreIndex:
    name: str
    lat: float
    lon: float
    radius_km: float
    level: int
    word: str
    components: list[Component] = field(default_factory=list)


def _presence(now: float, hist: Sequence[float]) -> Component:
    if len(hist) < MIN_BASELINE_H:
        return Component(
            "military",
            "Strategic military aircraft (6 h)",
            now,
            None,
            False,
            f"{now:.0f} now; learning the normal ({len(hist)} of {MIN_BASELINE_H} hours)",
        )
    med = statistics.median(hist)
    edge = max(1.5 * med, med + 3)
    return Component(
        "military",
        "Strategic military aircraft (6 h)",
        now,
        med,
        now >= edge,
        f"{now:.0f} now vs usual {med:.0f} (above normal from {edge:.0f})",
    )


def _count(key: str, label: str, n: int, refs: Sequence[str], at_least: int = 1) -> Component:
    shown = ", ".join(r for r in refs[:4] if r)
    return Component(
        key,
        label,
        float(n),
        0.0,
        n >= at_least,
        f"{n} open" + (f": {shown}" if shown else "") if n else "none open",
    )


def _conflict(today: int, days: Sequence[int]) -> Component:
    if len(days) < MIN_CONFLICT_DAYS:
        return Component(
            "conflict",
            "Conflict events in the news (24 h)",
            float(today),
            None,
            False,
            f"{today} in 24 h; learning the normal ({len(days)} of {MIN_CONFLICT_DAYS} days)",
        )
    med = statistics.median(days)
    edge = max(2 * med, med + 3)
    return Component(
        "conflict",
        "Conflict events in the news (24 h)",
        float(today),
        med,
        today >= edge,
        f"{today} in 24 h vs usual {med:.0f} a day (GDELT, 2+ sources; above from {edge:.0f})",
    )


def build(
    theatre: Mapping[str, Any],
    presence_now: float,
    presence_hist: Sequence[float],
    mil_alerts: Sequence[str],
    gnss_alerts: Sequence[str],
    spectrum_alerts: Sequence[str],
    conflict_today: int,
    conflict_days: Sequence[int],
    new_notices: int,
) -> TheatreIndex:
    comps = [
        _presence(presence_now, presence_hist),
        _count("mil_alerts", "Military indicator alerts", len(mil_alerts), mil_alerts),
        _count("gnss", "GPS interference alerts", len(gnss_alerts), gnss_alerts),
        _count("spectrum", "Radio band anomalies", len(spectrum_alerts), spectrum_alerts),
        _conflict(conflict_today, conflict_days),
        Component(
            "notices",
            "New airspace / GPS / military notices (24 h)",
            float(new_notices),
            None,
            new_notices >= 3,
            f"{new_notices} new" + (" (3 or more counts as above normal)" if new_notices else ""),
        ),
    ]
    level = min(3, sum(1 for c in comps if c.above))
    return TheatreIndex(
        name=str(theatre["name"]),
        lat=float(theatre["lat"]),
        lon=float(theatre["lon"]),
        radius_km=float(theatre["radius_km"]),
        level=level,
        word=LEVELS[level],
        components=comps,
    )
