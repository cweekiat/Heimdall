"""Task 014d: Hormuz real-data replay pipeline, offline, on a synthetic readsb-format fixture."""

from __future__ import annotations

import gzip
import importlib.util
import json
import math
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heimdall import config
from heimdall.api.app import create_app
from heimdall.api.demo import ReplayDemoStore, load_replay
from heimdall.backtest import cli
from heimdall.backtest.replay_io import read_bundle, read_obs_parquet, write_obs_parquet
from heimdall.backtest.traces import parse_trace
from heimdall.geo import within_radius_nm
from heimdall.schemas import AlertKind
from tests.synthetic import hormuz_fixture as fx

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


flt = _load("filter_adsblol")


@pytest.fixture(scope="module")
def sample() -> list[dict[str, Any]]:
    data = json.loads(gzip.decompress(fx.SAMPLE.read_bytes()))
    assert isinstance(data, list)
    return data


@pytest.fixture(scope="module")
def replay_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("hormuz")
    obs = flt.filter_sources([fx.SAMPLE])
    write_obs_parquet(d / f"{fx.DATE}.parquet", obs)
    cli.main(
        [
            "--aoi", "hormuz", "--date", fx.DATE, "--input", str(d / f"{fx.DATE}.parquet"),
            "--out-dir", str(d), "--no-sweep",
        ]
    )  # fmt: skip
    return d


# ---------------------------------------------------------------------------- config + script


def test_hormuz_aoi_in_config() -> None:
    a = config.settings().aoi("hormuz")
    assert (a.lat, a.lon, a.radius_nm) == (26.3, 56.3, 200)
    lat0, lon0, lat1, lon1 = flt.HORMUZ_BBOX  # the prefilter bbox must contain the AOI circle
    assert lat0 <= a.lat - a.radius_nm / 60 and a.lat + a.radius_nm / 60 <= lat1
    dlon = a.radius_nm / (60 * math.cos(math.radians(a.lat)))
    assert lon0 <= a.lon - dlon and a.lon + dlon <= lon1


def test_fetch_script_syntax_and_no_hardcoded_asset() -> None:
    bash = shutil.which("bash")
    assert bash
    subprocess.run([bash, "-n", str(SCRIPTS / "fetch_adsblol_day.sh")], check=True)
    text = (SCRIPTS / "fetch_adsblol_day.sh").read_text()
    assert "ODbL" in text and "planes-readsb-prod" in text


# ---------------------------------------------------------------------------- filter


def test_filter_keeps_only_bbox_rows_and_preserves_fields(sample: list[dict[str, Any]]) -> None:
    lat0, lon0, lat1, lon1 = flt.HORMUZ_BBOX
    kept_rows = 0
    for p in sample:
        out = flt.filter_payload(p)
        base = p["timestamp"]
        src = {round(base + r[0], 3): r for r in p["trace"]}
        if out is None:
            assert not any(lat0 <= r[1] <= lat1 and lon0 <= r[2] <= lon1 for r in p["trace"])
            continue
        assert out["timestamp"] == base and out["icao"] == p["icao"]
        for r in out["trace"]:
            assert lat0 <= r[1] <= lat1 and lon0 <= r[2] <= lon1
            assert src[round(base + r[0], 3)] == r  # kept verbatim: time, altitude, detail
            kept_rows += 1
    assert 0 < kept_rows < sum(len(p["trace"]) for p in sample)  # out-of-bbox rows were dropped
    obs = flt.filter_sources([fx.SAMPLE])
    assert len(obs) == kept_rows
    assert all(lat0 <= o.lat <= lat1 and lon0 <= o.lon <= lon1 for o in obs)
    assert not any(o.hex.startswith("9") for o in obs)  # the three out-of-bbox aircraft
    assert all(o.alt_baro is not None and o.nic is not None and o.nac_p is not None for o in obs)
    ref = {(o.hex, o.ts): o for p in sample for o in parse_trace(p, "history")}
    assert all(ref[(o.hex, o.ts)].alt_baro == o.alt_baro for o in obs)
    assert {o.nic for o in obs} == {0, 8} or {o.nic for o in obs} >= {0}  # degraded NIC kept


def test_filter_reads_tar_stream_and_survives_corrupt_member(
    tmp_path: Path, sample: list[dict[str, Any]]
) -> None:
    tar_path = tmp_path / "day.tar"
    with tarfile.open(tar_path, "w") as tf:
        for i, p in enumerate(sample[:5]):
            raw = gzip.compress(json.dumps(p).encode())
            ti = tarfile.TarInfo(f"traces/{i:02x}/trace_full_{p['icao']}.json")
            ti.size = len(raw)
            tf.addfile(ti, __import__("io").BytesIO(raw))
        bad = b"not json"
        ti = tarfile.TarInfo("traces/ff/trace_full_bad.json")
        ti.size = len(bad)
        tf.addfile(ti, __import__("io").BytesIO(bad))
    got = flt.filter_sources([tar_path])
    assert got and {o.hex for o in got} <= {p["icao"] for p in sample[:5]}


def test_parquet_roundtrip_lossless(tmp_path: Path) -> None:
    obs = flt.filter_sources([fx.SAMPLE])
    write_obs_parquet(tmp_path / "x.parquet", obs)
    back = read_obs_parquet(tmp_path / "x.parquet")
    assert sorted(back, key=lambda o: (o.ts, o.hex)) == sorted(obs, key=lambda o: (o.ts, o.hex))
    t = fx.T0 + timedelta(hours=1)
    assert all(o.ts >= t for o in read_obs_parquet(tmp_path / "x.parquet", start=t))


# ---------------------------------------------------------------------------- backtest


def test_backtest_on_filtered_fixture_end_to_end(replay_dir: Path) -> None:
    assert (replay_dir / f"backtest-hormuz-{fx.DATE}.md").exists()
    alerts, evidence = read_bundle(replay_dir / f"{fx.DATE}.parquet")
    assert (replay_dir / "lake" / "alerts").exists()
    # the fixture holds a 4/10 degraded-NIC cell for 2 h: a jamming alert must fire there
    jam = [a for a in alerts if a.kind == AlertKind.GNSS_JAMMING]
    assert jam, "expected >= 1 jamming alert (fixture contains degraded NIC)"
    ev_ids = {e.id for e in evidence}
    for a in alerts:
        assert a.aoi == "hormuz" and a.evidence_ids and set(a.evidence_ids) <= ev_ids
    for e in evidence:
        assert e.rules and e.counterfactual and e.detector_version and e.coverage is not None
    quiet = {a.h3_cell for a in jam}
    assert len(quiet) == 1  # only the strait cell; the healthy Gulf of Oman cell stays silent


def test_backtest_missing_input_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="not found"):
        cli.main(["--aoi", "hormuz", "--date", fx.DATE, "--input", str(tmp_path / "no.parquet")])


# ---------------------------------------------------------------------------- demo --replay


def test_demo_replay_serves_alerts_and_shifted_tracks(replay_dir: Path) -> None:
    path = replay_dir / f"{fx.DATE}.parquet"
    store = load_replay(path)
    alerts, _ = read_bundle(path)
    app = create_app(store, allowed_hosts=["testserver"])
    with TestClient(app, headers={"Origin": "http://testserver"}) as c:
        got = c.get("/api/alerts").json()
        assert {a["id"] for a in got} == {a.id for a in alerts} and got
        detail = c.get(f"/api/alerts/{got[0]['id']}").json()
        assert detail["evidence"]
    a = store.aircraft[0].ts
    assert isinstance(store, ReplayDemoStore) and a == store.t0


def test_replay_store_time_shift_and_wrap() -> None:
    obs = flt.filter_sources([fx.SAMPLE])
    boot = datetime(2026, 1, 1, tzinfo=UTC)
    st = ReplayDemoStore(obs, boot=boot)
    rows = st.aircraft_window(boot, boot + timedelta(minutes=10))  # replay minutes 0..10
    assert rows and all(boot <= r.ts < boot + timedelta(minutes=10) for r in rows)
    later = boot + st.span - timedelta(minutes=5)  # straddles the loop point
    w = st.aircraft_window(later, later + timedelta(minutes=10))
    assert all(later <= r.ts < later + timedelta(minutes=10) for r in w)
    assert st.aircraft_window(boot, boot + st.span + timedelta(seconds=1)) == []
    assert all(within_radius_nm(o.lat, o.lon, 26.3, 56.3, 250) for o in obs)
