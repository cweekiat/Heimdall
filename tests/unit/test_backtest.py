"""Backtest harness (task 006): trace parser, replay, GPSJam scoring, lead time, CLI report.

Offline: uses the committed synthetic fixtures in tests/fixtures/backtest (regenerate with
`uv run python -m tests.synthetic.backtest_fixtures`).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import h3
import pytest

from heimdall import config
from heimdall.backtest import cli
from heimdall.backtest.cli import SPARSE_PROFILE, _variant
from heimdall.backtest.replay import ReplayStore, replay
from heimdall.backtest.score import (
    GpsJamCell,
    daily_predictions,
    lead_times,
    load_gpsjam,
    score,
)
from heimdall.backtest.traces import iter_trace_dir, parse_trace, read_trace_file, to_trace
from heimdall.fuse.admiralty import grades_from_sources
from tests.synthetic import backtest_fixtures as fx

FIX = fx.ROOT
TRACES = FIX / "traces"
GPSJAM = FIX / f"gpsjam-{fx.DATE}-h3_4.csv"
DAY_END = fx.DAY + timedelta(days=1)


@pytest.fixture(scope="module")
def obs() -> list:
    return list(iter_trace_dir(TRACES))


@pytest.fixture(scope="module")
def base_replay(obs: list):
    s = config.settings()
    return replay(obs, s, "baltic", fx.T_START, fx.T_START + timedelta(hours=2.5))


# --------------------------------------------------------------------------- traces


def test_parse_trace_fields_and_edge_rows() -> None:
    payload = {
        "icao": "4CA7B5",
        "timestamp": 1756720800.0,
        "t": "K35R",
        "r": "58-0001",
        "dbFlags": 1,
        "trace": [
            [
                0,
                55.0,
                20.0,
                30000,
                450.0,
                90.0,
                0,
                64,
                {"nic": 0, "nac_p": 1, "type": "adsb_icao", "flight": "RCH1 "},
                "adsb_icao",
                30150,
            ],
            [10, 55.0, 20.1, 30000, 450.0, 90.0, 0, None, None],  # no detail: NIC unknown
            [20, 55.0, 20.2, 12000, 300.0, 90.0, 8 | 4, -500, None, "mlat"],  # geometric alt
            [30, 55.0, 20.3, "ground", 10.0, 90.0, 0, None, None, None],
            [40, 0.0, 0.0, 30000, 450.0, 90.0, 0, None, None],  # null island: dropped
            [50, 55.0, 20.4, 30000, 5000.0, 90.0, 0, None, None],  # implausible gs: dropped
        ],
    }
    rows = list(parse_trace(payload))
    assert len(rows) == 4
    a, b, c, d = rows
    assert a.hex == "4ca7b5" and a.provider == "history" and a.mil and a.type_code == "K35R"
    assert a.ts == datetime.fromtimestamp(1756720800, UTC)
    assert (a.nic, a.nac_p, a.alt_baro, a.alt_geom, a.flight) == (0, 1, 30000, 30150, "RCH1")
    assert a.src_type == "adsb_icao" and a.h3_r4 and a.baro_rate == 64
    assert b.nic is None and b.nac_p is None and b.src_type == "other"  # not forward-filled
    assert c.alt_baro is None and c.alt_geom == 12000 and c.geom_rate == -500
    assert c.src_type == "mlat"
    assert d.on_ground and d.alt_baro is None
    t0 = datetime.fromtimestamp(1756720810, UTC)
    assert len(list(parse_trace(payload, start=t0, end=t0 + timedelta(seconds=20)))) == 2
    assert list(parse_trace(payload, area=(10.0, 100.0, 50))) == []


def test_trace_roundtrip_and_gzip(tmp_path: Path) -> None:
    src = [o for o in fx.scenario() if o.hex == "a00000"]
    payload = to_trace(src)
    back = list(parse_trace(payload))
    assert [(o.ts, o.nic, o.src_type) for o in back] == [(o.ts, o.nic, o.src_type) for o in src]
    p = tmp_path / "trace_full_a00000.json"
    p.write_bytes(gzip.compress(json.dumps(payload).encode(), mtime=0))
    assert read_trace_file(p) == json.loads(json.dumps(payload))


def test_fixtures_are_current(tmp_path: Path) -> None:
    """Committed fixtures equal a fresh deterministic regeneration (byte for byte)."""
    for p in fx.write(tmp_path):
        rel = p.relative_to(tmp_path)
        assert (FIX / rel).read_bytes() == p.read_bytes(), rel


def test_fixture_traces_parse_completely(obs: list) -> None:
    assert len(obs) == len(fx.scenario())
    assert any(read_trace_file(f) for f in TRACES.rglob("*.json"))  # includes the gzipped one


# --------------------------------------------------------------------------- replay


def test_replay_store_window_matches_memory_semantics(obs: list) -> None:
    st = ReplayStore()
    st.insert_aircraft(list(reversed(obs)))
    lo, hi = fx.T_START + timedelta(minutes=10), fx.T_START + timedelta(minutes=40)
    got = st.aircraft_window(lo, hi)
    assert len(got) == sum(1 for o in obs if lo <= o.ts < hi)
    assert all(lo <= o.ts < hi for o in got)


def test_replay_positive_negative_and_edge_cells(base_replay) -> None:
    alerted = {a.h3_cell for a in base_replay.jamming_alerts()}
    ev_cells = {e.h3_cell for e in base_replay.jamming_evidence()}
    assert fx.cell("A") in alerted  # jammed cell
    assert fx.cell("D") not in ev_cells  # single faulty transponder never fires
    assert fx.cell("F") not in ev_cells  # MLAT-only cannot be judged
    assert fx.cell("E") not in ev_cells  # clean traffic
    assert fx.cell("C") not in alerted  # sparse: below min_aircraft 5
    assert all(a.severity == "info" for a in base_replay.alerts)  # control AOI cap
    assert base_replay.raised_at[next(iter(base_replay.jamming_alerts())).id] > fx.T_START


def test_replay_is_deterministic(obs: list, base_replay) -> None:
    again = replay(obs, config.settings(), "baltic", fx.T_START, fx.T_START + timedelta(hours=2.5))
    assert sorted(again.evidence) == sorted(base_replay.evidence)
    assert [a.model_dump() for a in again.alerts] == [a.model_dump() for a in base_replay.alerts]


def test_sparse_profile_catches_sparse_cell_with_capped_confidence(obs: list) -> None:
    s = config.settings()
    det = _variant(s.detectors_for(s.aoi("baltic")), **SPARSE_PROFILE)
    r = replay(obs, s, "baltic", fx.T_START, fx.T_START + timedelta(hours=2.5), detectors=det)
    assert fx.cell("C") in {a.h3_cell for a in r.jamming_alerts()}
    c_ev = [e for e in r.jamming_evidence() if e.h3_cell == fx.cell("C")]
    assert c_ev and all(e.coverage.confidence <= 0.5 for e in c_ev)
    assert fx.cell("D") not in {e.h3_cell for e in r.jamming_evidence()}


def test_per_aoi_detector_overrides_merge() -> None:
    s = config.settings()
    a = s.aoi("myanmar").model_copy(update={"detectors": {"jamming": {"min_aircraft": 3}}})
    merged = s.detectors_for(a)
    assert merged["jamming"]["min_aircraft"] == 3
    assert merged["jamming"]["ratio_alert"] == s.detectors["jamming"]["ratio_alert"]
    assert s.detectors["jamming"]["min_aircraft"] == 5  # global untouched


def test_sources_yaml_parses() -> None:
    grades = grades_from_sources(config.sources())
    assert grades["adsb_lol_history"] == "B" and "firms" in grades


# --------------------------------------------------------------------------- scoring


def test_gpsjam_levels_and_loading(tmp_path: Path) -> None:
    assert GpsJamCell("x", 9, 2).level() == "yellow"  # (2-1)/11 = 0.091
    assert GpsJamCell("x", 9, 1).level() == "green"  # de-noised to 0
    assert GpsJamCell("x", 8, 3).level() == "red"  # 2/11 = 0.18
    assert GpsJamCell("x", 1, 1).level(min_aircraft=3) == "gray"
    truth = load_gpsjam(GPSJAM)
    assert truth[fx.cell("A")].level() == "red" and truth[fx.cell("B")].level() == "yellow"
    # r5 rows aggregate into their r4 parent; alias column names accepted
    kids = sorted(h3.cell_to_children(fx.cell("A"), 5))[:2]
    p = tmp_path / "g.csv"
    p.write_text("h3,good,bad\n" + "".join(f"{k},5,1\n" for k in kids))
    agg = load_gpsjam(p)
    assert list(agg) == [fx.cell("A")] and (agg[fx.cell("A")].good, agg[fx.cell("A")].bad) == (
        10,
        2,
    )
    p.write_text(
        f"hex,count_good_aircraft,count_bad_aircraft\n{h3.cell_to_parent(kids[0], 3)},1,1\n"
    )
    with pytest.raises(ValueError):
        load_gpsjam(p)


def test_score_confusion_on_fixture_day(base_replay, obs: list) -> None:
    truth = load_gpsjam(GPSJAM)
    observed = {o.h3_r4 for o in obs if o.is_true_adsb and o.nic is not None}
    preds = daily_predictions(
        base_replay.jamming_evidence(), base_replay.jamming_alerts(), observed
    )
    s = score(truth, preds, config.settings().aoi("baltic"), min_aircraft=3)
    assert fx.cell("X") not in s.cells  # outside the AOI
    assert s.cells[fx.cell("G")] == ("red", "not_observed")
    assert s.cells[fx.cell("H")][0] == "gray"
    assert s.cells[fx.cell("D")] == ("green", "quiet")
    assert (s.red.tp, s.red.fp, s.red.fn) == (1, 0, 2)
    assert s.red.precision == 1.0 and s.red.recall == pytest.approx(1 / 3)
    assert sum(sum(v.values()) for v in s.confusion.values()) == len(s.cells)


def test_lead_times(base_replay) -> None:
    alerts = base_replay.jamming_alerts()
    t = base_replay.raised_at[alerts[0].id]
    early, late, miss = t + timedelta(minutes=30), t - timedelta(minutes=10), t + timedelta(days=3)
    got = lead_times(alerts, [miss, late, early], raised_at=base_replay.raised_at)
    assert [g.ref for g in got] == [late, early, miss]
    assert got[0].lead_s == -600.0 and got[1].lead_s == 1800.0
    assert got[2].first_alert is None and got[2].lead_s is None
    # without raised_at the first gated window start is used (earlier)
    assert lead_times(alerts, [early])[0].lead_s > 1800.0


# --------------------------------------------------------------------------- CLI


def _cli(out: Path) -> Path:
    return cli.main(
        [
            "--aoi", "baltic", "--date", fx.DATE, "--traces", str(TRACES),
            "--gpsjam", str(GPSJAM), "--refs", str(FIX / "refs.txt"),
            "--gpsjam-min-aircraft", "3", "--out-dir", str(out), "--note", "SYNTHETIC FIXTURE",
        ]
    )  # fmt: skip


def test_cli_offline_report_is_deterministic(tmp_path: Path) -> None:
    cfg = config.CONFIG_DIR / "config.yaml"
    before = hashlib.sha256(cfg.read_bytes()).hexdigest()
    a = _cli(tmp_path / "a")
    b = _cli(tmp_path / "b")
    assert a.name == f"backtest-baltic-{fx.DATE}.md"
    assert a.read_bytes() == b.read_bytes()
    text = a.read_text()
    for section in (
        "## Threshold sweep",
        "## Recommendation",
        "### Confusion table",
        "## Coverage and sparse-region proposal",
        "## Lead time",
        "proposals only",
    ):
        assert section in text, section
    sweep = text.split("## Threshold sweep")[1].split("## Recommendation")[0]
    assert sum(1 for line in sweep.splitlines() if line.startswith("| 0.")) == 12
    assert "0.1 (current) | 5" in sweep
    assert "**Proposal:" in text
    assert hashlib.sha256(cfg.read_bytes()).hexdigest() == before  # config never edited


def test_cli_rejects_unknown_aoi_and_missing_traces(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--aoi", "nowhere", "--date", fx.DATE, "--traces", str(TRACES)])
    with pytest.raises(SystemExit):
        cli.main(["--aoi", "baltic", "--date", fx.DATE, "--traces", str(tmp_path)])
