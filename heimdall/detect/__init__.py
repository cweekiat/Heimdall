"""Detector registry. Detectors are pure: (observations, cfg, now, baseline) -> list[Evidence]."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from heimdall.detect import ais_gap, ais_spoofing, jamming, orbit, spoofing, warning_area
from heimdall.schemas import AlertKind, Evidence


@dataclass(frozen=True)
class DetectorSpec:
    name: str
    version: str
    kind: AlertKind
    detect: Callable[..., list[Evidence]]
    baseline_metric: str | None = None


DETECTORS: dict[str, DetectorSpec] = {
    "jamming": DetectorSpec(
        "jamming", jamming.VERSION, AlertKind.GNSS_JAMMING, jamming.detect, jamming.METRIC
    ),
    "spoofing": DetectorSpec(
        "spoofing", spoofing.VERSION, AlertKind.GNSS_SPOOFING, spoofing.detect
    ),
    "orbit": DetectorSpec("orbit", orbit.VERSION, AlertKind.MIL_ORBIT, orbit.detect, orbit.METRIC),
    "ais_spoofing": DetectorSpec(
        "ais_spoofing", ais_spoofing.VERSION, AlertKind.AIS_SPOOFING, ais_spoofing.detect
    ),
    "ais_gap": DetectorSpec("ais_gap", ais_gap.VERSION, AlertKind.AIS_GAP, ais_gap.detect),
    # notices: detect(events, cfg, now, *, aoi) - context-only, never opens an alert
    "warning_area": DetectorSpec(
        "warning_area", warning_area.VERSION, AlertKind.WARNING_AREA, warning_area.detect
    ),
}

__all__ = [
    "DETECTORS",
    "DetectorSpec",
    "ais_gap",
    "ais_spoofing",
    "jamming",
    "orbit",
    "spoofing",
    "warning_area",
]
