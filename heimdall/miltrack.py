"""Military aircraft track store (task 039). Pure data structure; the pipeline does the I/O.

Worldwide display rows stay only ~1 h in the hot store, but build-up detection needs days. This
keeps, per military aircraft (ICAO hex), points decimated to one per `step_s` (default 5 min) for
`keep_h` (default 72 h): ~300 aircraft x 864 points at most. Points are tuples (epoch s, lat, lon,
alt ft or -1 on ground / unknown, gs kt or -1). `to_json` / `from_json` persist it across restarts
(`data/state/mil_tracks.json`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from heimdall.schemas import AircraftObs

Point = tuple[float, float, float, float, float]  # t, lat, lon, alt_ft (-1 = ground/unknown), gs


@dataclass
class MilTrack:
    hex: str
    type_code: str | None = None
    flight: str | None = None
    registration: str | None = None
    points: list[Point] = field(default_factory=list)


@dataclass
class MilTracks:
    step_s: float = 300.0
    keep_h: float = 72.0
    tracks: dict[str, MilTrack] = field(default_factory=dict)
    last_ts: float = 0.0  # newest observation time absorbed (incremental reads)

    def add(self, obs: Iterable[AircraftObs]) -> int:
        """Absorb observations (any order); returns points added."""
        n = 0
        for o in sorted(obs, key=lambda o: o.ts):
            t = o.ts.timestamp()
            tr = self.tracks.get(o.hex)
            if tr is None:
                tr = self.tracks[o.hex] = MilTrack(o.hex)
            tr.type_code = o.type_code or tr.type_code
            tr.flight = o.flight or tr.flight
            tr.registration = o.registration or tr.registration
            if tr.points and t - tr.points[-1][0] < self.step_s:
                continue
            alt = -1.0 if o.on_ground or o.alt_baro is None else float(o.alt_baro)
            tr.points.append((t, o.lat, o.lon, alt, float(o.gs) if o.gs is not None else -1.0))
            self.last_ts = max(self.last_ts, t)
            n += 1
        return n

    def prune(self, now: datetime) -> None:
        lo = now.timestamp() - self.keep_h * 3600
        for h in list(self.tracks):
            pts = [p for p in self.tracks[h].points if p[0] >= lo]
            if pts:
                self.tracks[h].points = pts
            else:
                del self.tracks[h]

    def to_json(self) -> str:
        return json.dumps(
            {
                "step_s": self.step_s,
                "keep_h": self.keep_h,
                "last_ts": self.last_ts,
                "tracks": {
                    h: [t.type_code, t.flight, t.registration, t.points]
                    for h, t in self.tracks.items()
                },
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str, **kw: Any) -> MilTracks:
        d = json.loads(text)
        out = cls(**kw)
        out.last_ts = float(d.get("last_ts", 0.0))
        for h, (tc, fl, reg, pts) in (d.get("tracks") or {}).items():
            out.tracks[str(h)] = MilTrack(
                str(h),
                tc,
                fl,
                reg,
                [tuple(float(x) for x in p) for p in pts if len(p) == 5],  # type: ignore[misc]
            )
        return out
