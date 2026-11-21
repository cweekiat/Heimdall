"""GNSS interference map, GPSJam method (task 031). Pure: no I/O.

Per H3 r4 cell and period (a UTC day or a rolling 24 h): aircraft with at least one NIC/NACp report
are counted once per cell; an aircraft is *bad* in a cell when at least `dwell_frac` of its known
reports there are degraded (the jamming detector's rule, `detect.jamming.degraded`). As GPSJam:
    pct_bad = 100 * max(0, bad - 1) / total
The -1 suppresses a single faulty transponder, at the cost of hiding thin cells. Classes: green
< 2 %, yellow 2-10 %, red > 10 %. Cells without such aircraft are absent (no data is not green).
Military aircraft are excluded from `bad` by default: GPSJam's FAQ notes trainers blocking their own
antennas in hard manoeuvres. The map is descriptive context and never creates alerts.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from heimdall.detect.jamming import degraded
from heimdall.geo import h3_cell, h3_center
from heimdall.schemas import AircraftObs

GpsjamClass = Literal["green", "yellow", "red"]


@dataclass
class AcCount:
    """Report counts of one aircraft in one cell over the period."""

    cell: str
    hex: str
    known: int
    bad: int
    mil: bool = False
    providers: tuple[str, ...] = ()


@dataclass
class GnssCell:
    h3: str
    good: int
    bad: int
    total: int
    pct: float
    cls: GpsjamClass
    mil_excluded: int = 0
    providers: list[str] = field(default_factory=list)


@dataclass
class GnssDay:
    date: str  # YYYY-MM-DD or "last24h"
    cells: list[GnssCell]
    hours_with_data: int = 24
    incomplete: bool = False
    sources: list[str] = field(default_factory=list)


def gpsjam_class(pct: float) -> GpsjamClass:
    return "green" if pct < 2 else "yellow" if pct <= 10 else "red"


def counts_from_obs(
    obs: Iterable[AircraftObs], nic_below: int = 7, nacp_below: int = 8
) -> list[AcCount]:
    """Airborne true-ADS-B reports -> per (cell, aircraft) counts (the Store/demo path)."""
    acc: dict[tuple[str, str], AcCount] = {}
    provs: dict[tuple[str, str], set[str]] = {}
    for o in obs:
        if o.on_ground or not o.is_true_adsb:
            continue
        d = degraded(o, nic_below, nacp_below)
        if d is None:
            continue
        cell = o.h3_r4 or h3_cell(o.lat, o.lon, 4)  # demo movers leave h3_r4 empty
        k = (cell, o.hex)
        a = acc.get(k)
        if a is None:
            a = acc[k] = AcCount(cell, o.hex, 0, 0, o.mil)
            provs[k] = set()
        a.known += 1
        a.bad += int(d)
        a.mil = a.mil or o.mil
        provs[k].add(o.provider)
    for k, a in acc.items():
        a.providers = tuple(sorted(provs[k]))
    return list(acc.values())


def aggregate(
    counts: Iterable[AcCount], *, dwell_frac: float = 0.5, exclude_military: bool = True
) -> list[GnssCell]:
    """Per-cell GPSJam numbers from per-aircraft counts."""
    by_cell: dict[str, list[AcCount]] = {}
    for a in counts:
        if a.known > 0:
            by_cell.setdefault(a.cell, []).append(a)
    out: list[GnssCell] = []
    for cell, acs in by_cell.items():
        is_bad = [a.bad / a.known >= dwell_frac for a in acs]
        mil_ex = sum(1 for a, b in zip(acs, is_bad, strict=True) if b and a.mil)
        dropped = mil_ex if exclude_military else 0  # degraded military: left out entirely
        bad = sum(is_bad) - dropped
        total = len(acs) - dropped
        if total <= 0:
            continue
        pct = 100.0 * max(0, bad - 1) / total
        out.append(
            GnssCell(
                h3=cell,
                good=total - bad,
                bad=bad,
                total=total,
                pct=round(pct, 2),
                cls=gpsjam_class(pct),
                mil_excluded=mil_ex if exclude_military else 0,
                providers=sorted({p for a in acs for p in a.providers}),
            )
        )
    return sorted(out, key=lambda c: c.h3)


def in_bbox(cells: Sequence[GnssCell], bbox: tuple[float, float, float, float]) -> list[GnssCell]:
    """Cells whose centre is in (west, south, east, north); west > east wraps the antimeridian."""
    w, s, e, n = bbox
    out = []
    for c in cells:
        lat, lon = h3_center(c.h3)
        if not s <= lat <= n:
            continue
        if (w <= lon <= e) if w <= e else (lon >= w or lon <= e):
            out.append(c)
    return out


@dataclass
class Comparison:
    n_both: int
    agree: int
    confusion: dict[tuple[GpsjamClass, GpsjamClass], int]  # (ours, theirs) -> count
    worst: list[tuple[str, float, float]]  # (h3, our pct, their pct), biggest gaps first

    @property
    def agreement(self) -> float:
        return self.agree / self.n_both if self.n_both else 0.0


def gpsjam_rows(rows: Iterable[dict[str, str]]) -> dict[str, tuple[int, int]]:
    """GPSJam CSV rows (hex, count_good_aircraft, count_bad_aircraft) -> h3 -> (good, bad)."""
    out: dict[str, tuple[int, int]] = {}
    for r in rows:
        try:
            out[r["hex"].strip()] = (int(r["count_good_aircraft"]), int(r["count_bad_aircraft"]))
        except (KeyError, ValueError):
            continue
    return out


def compare(
    ours: Sequence[GnssCell], theirs: dict[str, tuple[int, int]], min_aircraft: int = 5
) -> Comparison:
    """Class agreement on cells where both sides have >= min_aircraft aircraft."""
    conf: dict[tuple[GpsjamClass, GpsjamClass], int] = {}
    gaps: list[tuple[str, float, float]] = []
    n = agree = 0
    for c in ours:
        t = theirs.get(c.h3)
        if t is None or c.total < min_aircraft or sum(t) < min_aircraft:
            continue
        their_pct = 100.0 * max(0, t[1] - 1) / sum(t)
        a, b = c.cls, gpsjam_class(their_pct)
        conf[(a, b)] = conf.get((a, b), 0) + 1
        n += 1
        agree += int(a == b)
        gaps.append((c.h3, c.pct, round(their_pct, 2)))
    gaps.sort(key=lambda g: -abs(g[1] - g[2]))
    return Comparison(n, agree, conf, gaps[:10])
