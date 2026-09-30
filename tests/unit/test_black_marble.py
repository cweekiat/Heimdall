"""Black Marble VNP46A2 reduction (task 019): synthetic tiles built in tmp_path, respx, offline."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx

from heimdall.config import AOI
from heimdall.geo import bbox_for_radius, h3_cell
from heimdall.ingest.black_marble import (
    BlackMarbleConnector,
    reduce_tile,
    tiles_for_aois,
    tiles_for_bbox,
)
from heimdall.ingest.http import RateLimitedClient

_spec = importlib.util.spec_from_file_location(
    "bm_make_tile", Path(__file__).parent.parent / "fixtures" / "black_marble" / "make_tile.py"
)
assert _spec is not None and _spec.loader is not None
mt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mt)

BASE = "https://ladsweb.example.invalid"
FAKE_CRED = "not-a-real-value-0001"
DAY = date(2026, 1, 9)
NAME = "VNP46A2.A2026009.h27v06.002.2026010123456.h5"
# tile h27v06 covers lat 20..30, lon 90..100; 240x240 px -> 1/24 degree per pixel
AOIS = [AOI(name="t", lat=25.0, lon=95.0, radius_nm=100)]
CFG: dict[str, Any] = {"good_qf": [0, 1], "product": "VNP46A2", "keep_failed_h": 24}


def _px_cell(i: int, j: int, n: int = 240) -> str:
    lat = 30.0 - (i + 0.5) * 10.0 / n
    lon = 90.0 + (j + 0.5) * 10.0 / n
    return h3_cell(lat, lon, 4)


def _cell_pixels(cell: str, n: int = 240) -> list[tuple[int, int]]:
    return [(i, j) for i in range(n) for j in range(n) if _px_cell(i, j, n) == cell]


def _full_cell(n: int = 240) -> tuple[str, list[tuple[int, int]]]:
    """A cell at the middle of the tile with all of its pixels inside the tile."""
    cell = _px_cell(n // 2, n // 2, n)
    return cell, _cell_pixels(cell, n)


def test_tiles_for_bbox_myanmar_and_baltic() -> None:
    myanmar = AOI(name="myanmar", lat=20.5, lon=96.5, radius_nm=250)
    baltic = AOI(name="baltic", lat=56.0, lon=20.5, radius_nm=250)
    assert tiles_for_aois([myanmar]) == ["h27v06", "h27v07", "h28v06", "h28v07"]
    assert tiles_for_aois([baltic]) == ["h19v02", "h19v03", "h20v02", "h20v03"]
    assert tiles_for_bbox((25.0, 95.0, 25.5, 95.5)) == ["h27v06"]
    assert tiles_for_bbox((-89.9, -179.9, -80.1, -170.1)) == ["h00v17"]
    assert tiles_for_bbox(bbox_for_radius(0.0, 0.0, 10)[0]) == [
        "h17v08",
        "h18v08",
        "h17v09",
        "h18v09",
    ]


def test_mean_and_exclusions(tmp_path: Path) -> None:
    cell, px = _full_cell()
    assert len(px) >= 20
    bad = px[:5]  # bad QF, cloud, snow, fill, and a high-QF-value pixel

    def edit(a: dict[str, np.ndarray]) -> None:
        # excluded pixels carry a huge value that would wreck the mean if it were used
        (i0, j0), (i1, j1), (i2, j2), (i3, j3), (i4, j4) = bad
        a["ntl"][i0, j0] = 20000
        a["qf"][i0, j0] = 2  # cloud/poor quality
        a["ntl"][i1, j1] = 20000
        a["cloud"][i1, j1] = mt.CLOUD_CONFIDENT
        a["ntl"][i2, j2] = 20000
        a["snow"][i2, j2] = 1
        a["ntl"][i3, j3] = mt.FILL
        a["ntl"][i4, j4] = 20000
        a["qf"][i4, j4] = 255
        # one lit pixel at twice the radiance, and one poor-quality-but-allowed (qf 1) pixel
        k0, l0 = px[5]
        a["ntl"][k0, l0] = 2000

    p = tmp_path / NAME
    mt.make_tile(p, edit=edit)
    events = {e.attrs["h3_r4"]: e for e in reduce_tile(p, DAY, AOIS, CFG)}
    ev = events[cell]
    n_total = len(px)
    good = [1000.0] * (n_total - 5)
    good[0] = 2000.0
    assert ev.kind == "night_lights"
    assert ev.source == "black_marble"
    assert ev.attrs["n_total"] == n_total
    assert ev.attrs["n_valid"] == n_total - 5
    assert ev.attrs["valid_frac"] == pytest.approx((n_total - 5) / n_total)
    assert ev.value == pytest.approx(np.mean(good) * mt.SCALE)
    assert ev.attrs["median"] == pytest.approx(100.0)
    assert ev.attrs["p90"] == pytest.approx(np.percentile(good, 90) * mt.SCALE)
    assert ev.attrs["cloud_frac"] == pytest.approx(1 / n_total)
    assert ev.attrs["role"] == "signal"
    assert ev.id == f"black_marble|VNP46A2|2026-01-09|{cell}"
    assert ev.ts == datetime(2026, 1, 9, tzinfo=UTC)


def test_uniform_tile_and_gap_filled_ignored(tmp_path: Path) -> None:
    p = tmp_path / NAME
    mt.make_tile(p)  # gap-filled dataset present with radiance 3000, must be ignored
    events = reduce_tile(p, DAY, AOIS, CFG)
    assert events
    for ev in events:
        assert ev.value == pytest.approx(100.0)
        assert ev.attrs["valid_frac"] == 1.0
        assert ev.attrs["n_valid"] == ev.attrs["n_total"] > 0
        assert AOIS[0].contains(ev.lat or 0.0, ev.lon or 0.0)


def test_good_qf_config_and_fully_invalid_cell(tmp_path: Path) -> None:
    cell, px = _full_cell()

    def edit(a: dict[str, np.ndarray]) -> None:
        for i, j in px:
            a["qf"][i, j] = 1

    p = tmp_path / NAME
    mt.make_tile(p, edit=edit)
    ev = {e.attrs["h3_r4"]: e for e in reduce_tile(p, DAY, AOIS, {**CFG, "good_qf": [0]})}[cell]
    assert ev.value is None and ev.attrs["n_valid"] == 0 and ev.attrs["valid_frac"] == 0.0
    assert ev.attrs["n_total"] == len(px) and ev.attrs["median"] is None
    ev = {e.attrs["h3_r4"]: e for e in reduce_tile(p, DAY, AOIS, CFG)}[cell]
    assert ev.attrs["n_valid"] == len(px)


def test_only_aoi_cells_emitted(tmp_path: Path) -> None:
    p = tmp_path / NAME
    mt.make_tile(p)
    far = [AOI(name="far", lat=27.0, lon=98.5, radius_nm=30)]
    for ev in reduce_tile(p, DAY, far, CFG):
        assert far[0].contains(ev.lat or 0.0, ev.lon or 0.0)
    assert reduce_tile(p, DAY, [AOI(name="none", lat=-30, lon=10, radius_nm=50)], CFG) == []


def _conn(tmp_path: Path, token: str | None = FAKE_CRED, **kw: Any) -> BlackMarbleConnector:
    return BlackMarbleConnector(
        "black_marble",
        BASE,
        AOIS,
        token,
        cfg={**CFG, "lookback_days": 1, "collection": "5200"},
        cache_dir=tmp_path / "cache" / "black_marble",
        client=RateLimitedClient("black_marble", rps=1000, max_retries=0),
        now=lambda: datetime(2026, 1, 10, 12, tzinfo=UTC),
        **kw,
    )


@respx.mock
async def test_poll_downloads_reduces_and_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tile = tmp_path / "src.h5"
    mt.make_tile(tile)
    day = f"{BASE}/archive/allData/5200/VNP46A2/2026/009"
    listing = respx.get(f"{day}.json").mock(
        return_value=httpx.Response(200, json=[{"name": NAME}, {"name": "other.h27v99.h5"}])
    )
    dl = respx.get(f"{day}/{NAME}").mock(
        return_value=httpx.Response(200, content=tile.read_bytes())
    )
    c = _conn(tmp_path)
    events = await c.poll_once()
    assert events and all(e.kind == "night_lights" for e in events)
    assert listing.calls.last.request.headers["Authorization"] == f"Bearer {FAKE_CRED}"
    assert dl.calls.last.request.headers["Authorization"] == f"Bearer {FAKE_CRED}"
    assert list((tmp_path / "cache" / "black_marble").glob("*")) == []  # tile deleted
    assert not (tmp_path / "data" / "lake").exists()
    assert FAKE_CRED not in repr(c.health())
    assert c.health().status == "ok"
    assert await c.poll_once() == []  # (date, tile) already reduced: no second download
    assert dl.call_count == 1
    await c.aclose()


@respx.mock
async def test_bad_tile_kept_then_cleaned(tmp_path: Path) -> None:
    day = f"{BASE}/archive/allData/5200/VNP46A2/2026/009"
    respx.get(f"{day}.json").mock(return_value=httpx.Response(200, json=[{"name": NAME}]))
    respx.get(f"{day}/{NAME}").mock(return_value=httpx.Response(200, content=b"not hdf5"))
    c = _conn(tmp_path)
    assert await c.poll_once() == []
    kept = tmp_path / "cache" / "black_marble" / NAME
    assert kept.exists() and c.health().status == "degraded"
    import os

    old = kept.stat().st_mtime - 25 * 3600
    os.utime(kept, (old, old))
    c.cleanup_cache()
    assert not kept.exists()
    await c.aclose()


async def test_needs_key_makes_no_request(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as mock:
        c = _conn(tmp_path, token=None)
        assert c.health().status == "needs_key"
        assert await c.poll_once() == []
        assert [b async for b in c.stream()] == []
        assert mock.calls.call_count == 0


@respx.mock
async def test_unauthorized(tmp_path: Path) -> None:
    respx.get(f"{BASE}/archive/allData/5200/VNP46A2/2026/009.json").mock(
        return_value=httpx.Response(401)
    )
    c = _conn(tmp_path)
    assert await c.poll_once() == []
    assert c.health().status == "down"
    await c.aclose()
