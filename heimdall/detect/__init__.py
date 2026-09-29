"""Detector registry. Detectors are pure: (observations, cfg, now, baseline) -> list[Evidence]."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from heimdall.detect import jamming, orbit, spoofing
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
}

__all__ = ["DETECTORS", "DetectorSpec", "jamming", "orbit", "spoofing"]
