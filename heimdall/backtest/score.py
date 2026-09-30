"""Scoring a replay against GPSJam daily cells, and alert lead time vs reference timestamps.

GPSJam day file (assumed format, to be confirmed by task 005 — docs/sources.md "gpsjam"): CSV with
an H3 cell column (`hex`) and per-day aircraft counts (`count_good_aircraft`,
`count_bad_aircraft`). Aliases `h3`/`cell`, `good`/`n_good`, `bad`/`n_bad` are accepted. Cells finer
than r4 are aggregated to their r4 parent (counts summed); coarser cells are rejected.

GPSJam level of a cell (same de-noising as our detector, docs/detectors.md):
    ratio = max(0, bad - 1) / (good + bad)
    red    if ratio > 0.10
    yellow if 0.02 < ratio <= 0.10
    green  otherwise
    gray   if good + bad < `min_aircraft` (insufficient data; excluded from scoring)

Our daily prediction per r4 cell:
    alert          a gnss_jamming alert exists for the cell (Evidence passed the persistence gate)
    evidence_only  jamming Evidence fired but never persisted into an alert
    quiet          the cell had quality (true ADS-B with NIC/NACp) aircraft but no Evidence
    not_observed   no quality aircraft in the cell all day (coverage gap)

Binary scoring at two truth levels, positive prediction = `alert`:
    red      truth = GPSJam red
    yellow+  truth = GPSJam yellow or red (> 2 %)
Only cells with a GPSJam level other than gray, whose centre lies within the AOI radius, are
scored. precision = TP/(TP+FP), recall = TP/(TP+FN), F1 = 2PR/(P+R); undefined -> None.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import h3

from heimdall.config import AOI
from heimdall.geo import h3_center, within_radius_nm
from heimdall.schemas import Alert, AlertKind, Evidence

Level = Literal["red", "yellow", "green", "gray"]
Pred = Literal["alert", "evidence_only", "quiet", "not_observed"]
LEVELS: tuple[Level, ...] = ("red", "yellow", "green", "gray")
PREDS: tuple[Pred, ...] = ("alert", "evidence_only", "quiet", "not_observed")
RED_ABOVE = 0.10
YELLOW_ABOVE = 0.02

_CELL = ("hex", "h3", "cell", "h3_index")
_GOOD = ("count_good_aircraft", "good", "n_good")
_BAD = ("count_bad_aircraft", "bad", "n_bad")


@dataclass(frozen=True)
class GpsJamCell:
    cell: str
    good: int
    bad: int

    @property
    def total(self) -> int:
        return self.good + self.bad

    @property
    def ratio(self) -> float:
        return max(0, self.bad - 1) / self.total if self.total else 0.0

    def level(self, min_aircraft: int = 1) -> Level:
        if self.total < max(1, min_aircraft):
            return "gray"
        if self.ratio > RED_ABOVE:
            return "red"
        if self.ratio > YELLOW_ABOVE:
            return "yellow"
        return "green"


def _col(header: Sequence[str], names: Sequence[str], path: str | Path) -> str:
    low = {h.strip().lower(): h for h in header}
    for n in names:
        if n in low:
            return low[n]
    raise ValueError(f"{path}: none of the columns {names} found in {list(header)}")


def load_gpsjam(path: str | Path, res: int = 4) -> dict[str, GpsJamCell]:
    """Read a GPSJam day CSV into r`res` cells (finer cells summed into their parent)."""
    good: dict[str, int] = {}
    bad: dict[str, int] = {}
    with Path(path).open(newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        c_cell, c_good, c_bad = (_col(header, n, path) for n in (_CELL, _GOOD, _BAD))
        for row in reader:
            cell = (row[c_cell] or "").strip().lower()
            if not cell:
                continue
            r = h3.get_resolution(cell)
            if r < res:
                raise ValueError(f"{path}: cell {cell} is r{r}, coarser than r{res}")
            if r > res:
                cell = str(h3.cell_to_parent(cell, res))
            good[cell] = good.get(cell, 0) + int(float(row[c_good] or 0))
            bad[cell] = bad.get(cell, 0) + int(float(row[c_bad] or 0))
    return {c: GpsJamCell(c, good[c], bad[c]) for c in sorted(good)}


@dataclass
class CellPrediction:
    cell: str
    pred: Pred
    n_evidence: int = 0
    max_ratio: float = 0.0


def daily_predictions(
    evidence: Iterable[Evidence], alerts: Iterable[Alert], observed_cells: Iterable[str]
) -> dict[str, CellPrediction]:
    """Collapse a replay day into one prediction per r4 cell (see module docstring)."""
    out: dict[str, CellPrediction] = {
        c: CellPrediction(c, "quiet") for c in sorted(set(observed_cells))
    }
    for e in evidence:
        if e.kind != AlertKind.GNSS_JAMMING or not e.h3_cell:
            continue
        p = out.setdefault(e.h3_cell, CellPrediction(e.h3_cell, "quiet"))
        p.n_evidence += 1
        p.max_ratio = max(p.max_ratio, e.features.get("ratio", 0.0))
        if p.pred != "alert":
            p.pred = "evidence_only"
    for a in alerts:
        if a.kind == AlertKind.GNSS_JAMMING and a.h3_cell:
            out.setdefault(a.h3_cell, CellPrediction(a.h3_cell, "quiet")).pred = "alert"
    return out


@dataclass
class BinaryScore:
    level: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float | None:
        d = self.tp + self.fp
        return self.tp / d if d else None

    @property
    def recall(self) -> float | None:
        d = self.tp + self.fn
        return self.tp / d if d else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        return 2 * p * r / (p + r) if p + r else 0.0


@dataclass
class ScoreReport:
    aoi: str
    red: BinaryScore
    yellow: BinaryScore
    confusion: dict[str, dict[str, int]]  # truth level (+ "no_data") -> prediction -> count
    cells: dict[str, tuple[str, Pred]] = field(default_factory=dict)  # cell -> (truth, pred)


def in_aoi(cell: str, aoi: AOI) -> bool:
    lat, lon = h3_center(cell)
    return within_radius_nm(lat, lon, aoi.lat, aoi.lon, aoi.radius_nm)


def score(
    truth: Mapping[str, GpsJamCell],
    preds: Mapping[str, CellPrediction],
    aoi: AOI,
    *,
    min_aircraft: int = 1,
) -> ScoreReport:
    """Precision/recall/F1 at red and yellow+ plus the confusion table for one AOI."""
    rows = [*LEVELS, "no_data"]
    confusion: dict[str, dict[str, int]] = {r: dict.fromkeys(PREDS, 0) for r in rows}
    red, yellow = BinaryScore("red"), BinaryScore("yellow+")
    cells: dict[str, tuple[str, Pred]] = {}
    universe = sorted(c for c in set(truth) | set(preds) if in_aoi(c, aoi))
    for c in universe:
        t = truth.get(c)
        lvl = t.level(min_aircraft) if t else "no_data"
        pred: Pred = preds[c].pred if c in preds else "not_observed"
        confusion[lvl][pred] += 1
        cells[c] = (lvl, pred)
        if lvl in ("gray", "no_data"):
            continue
        hit = pred == "alert"
        for bs, pos in ((red, lvl == "red"), (yellow, lvl in ("red", "yellow"))):
            if pos and hit:
                bs.tp += 1
            elif pos:
                bs.fn += 1
            elif hit:
                bs.fp += 1
            else:
                bs.tn += 1
    return ScoreReport(aoi=aoi.name, red=red, yellow=yellow, confusion=confusion, cells=cells)


@dataclass(frozen=True)
class LeadTime:
    ref: datetime
    first_alert: datetime | None  # earliest matching alert time, None = missed
    lead_s: float | None  # ref - first_alert (> 0: we alerted before the reference)
    alert_id: str | None


def lead_times(
    alerts: Sequence[Alert],
    refs: Sequence[datetime],
    *,
    kind: AlertKind | str | None = AlertKind.GNSS_JAMMING,
    cells: Iterable[str] | None = None,
    raised_at: Mapping[str, datetime] | None = None,
    max_lead: timedelta = timedelta(hours=24),
    max_lag: timedelta = timedelta(hours=6),
) -> list[LeadTime]:
    """For each reference time (NOTAM issue, news report ...) the first alert time around it.

    Alert time = `raised_at[alert.id]` (the replay tick at which the alert first existed, i.e.
    when an analyst could have seen it) or, if absent, `first_seen` (start of the first gated
    window — earlier than the alert could really have been raised). An alert matches a reference
    if it has the given kind (None = any), lies in `cells` (if given) and its time is in
    [ref - max_lead, ref + max_lag]. lead_s = ref - alert time: positive when Heimdall alerted
    before the reference, negative when it lagged. Pure.
    """
    cellset = set(cells) if cells is not None else None
    raised = raised_at or {}
    cands = sorted(
        (
            (raised.get(a.id, a.first_seen), a.id)
            for a in alerts
            if (kind is None or a.kind == kind)
            and (cellset is None or (a.h3_cell or "") in cellset)
        ),
    )
    out = []
    for ref in sorted(refs):
        hit = next(((t, i) for t, i in cands if ref - max_lead <= t <= ref + max_lag), None)
        out.append(
            LeadTime(
                ref=ref,
                first_alert=hit[0] if hit else None,
                lead_s=(ref - hit[0]).total_seconds() if hit else None,
                alert_id=hit[1] if hit else None,
            )
        )
    return out
