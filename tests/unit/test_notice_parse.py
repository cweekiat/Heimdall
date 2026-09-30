from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from heimdall.geo import haversine_km
from heimdall.ingest.notice_parse import (
    categorize,
    circle_polygon,
    covering_cells,
    make_ref,
    parse_coords,
    parse_dtg,
    parse_qline,
    parse_shape,
    parse_validity,
    sanitize,
)

RULES = {
    "gnss_interference": ["gps unreliable", "gnss interference", "gps jamming"],
    "live_firing": ["live firing", "实弹射击"],
    "military_exercise": ["military exercise", "军事演习"],
}


@pytest.mark.parametrize(
    ("text", "lat", "lon"),
    [
        ("12-34.56N 113-45.67E", 12.576, 113.7612),
        ("12-34N 113-45E", 12.5667, 113.75),
        ("12°34.5'N 113°45.6'E", 12.575, 113.76),
        ("18°10′00″N 109°30′00″E", 18.1667, 109.5),
        ("123456N 1134512E", 12.5822, 113.7533),
        ("Q) VYYF/QRRCA/IV/BO/W/000/250/ 1234N11345E", 12.5667, 113.75),
        ("12.5N 113.5E", 12.5, 113.5),
        ("北纬18度10分、东经109度30分", 18.1667, 109.5),
        ("18-10.00N/109-30.00E", 18.1667, 109.5),
        ("05-30S 010-15W", -5.5, -10.25),
        ("１２－３４Ｎ １１３－４５Ｅ", None, None),  # full-width: only after sanitize()
    ],
)
def test_coordinate_formats(text: str, lat: float | None, lon: float | None) -> None:
    if lat is None:
        got = parse_coords(sanitize(text.replace("－", "-")))
        assert got and got[0] == pytest.approx((12.5667, 113.75), abs=1e-3)
        return
    got = parse_coords(text)
    assert len(got) == 1
    assert got[0] == pytest.approx((lat, lon), abs=1e-3)


@pytest.mark.parametrize(
    "text",
    [
        "95-00N 113-45E",
        "12-34N 190-45E",
        "12-75N 113-45E",
        "9999N 99999E",
        "北纬95度东经10度",
        "N E",
    ],
)
def test_out_of_range_rejected(text: str) -> None:
    assert parse_coords(text) == []


def test_polygon_closed_and_ccw() -> None:
    text = "AREA BOUNDED BY 18-10N 109-30E, 17-20N 109-30E, 17-20N 110-30E, 18-10N 110-30E"  # given clockwise
    geom, status = parse_shape(text)
    assert status == "ok" and geom and geom["type"] == "Polygon"
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 5
    area = sum(
        ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)
    )
    assert area > 0  # counter-clockwise


@pytest.mark.parametrize(
    ("text", "nm"),
    [
        ("LIVE FIRING WITHIN 10 NM OF 12-34.56N 113-45.67E", 10.0),
        ("WITHIN 25 MILES OF 1300N 12000E", 25.0),
        ("以北纬16度30分、东经112度00分为圆心，半径5海里", 5.0),
    ],
)
def test_circle_radius_within_1pct(text: str, nm: float) -> None:
    geom, status = parse_shape(text)
    assert status == "ok" and geom and geom["type"] == "Polygon"
    ring = geom["coordinates"][0]
    assert len(ring) == 33  # 32-gon, closed
    clat = sum(p[1] for p in ring[:-1]) / 32
    clon = sum(p[0] for p in ring[:-1]) / 32
    for lon, lat in ring[:-1]:
        assert haversine_km(clat, clon, lat, lon) / 1.852 == pytest.approx(nm, rel=0.01)


def test_qline_circle_and_altitudes() -> None:
    text = "Q) VYYF/QRRCA/IV/BO/W/000/250/1234N09612E025\nE) X"
    lat, lon, rad, lo, hi = parse_qline(text) or (0, 0, 0, 0, 0)
    assert (rad, lo, hi) == (25.0, 0, 25000)
    geom, status = parse_shape(text)
    assert status == "ok" and geom
    ring = geom["coordinates"][0]
    assert haversine_km(lat, lon, ring[0][1], ring[0][0]) / 1.852 == pytest.approx(25, rel=0.01)


def test_points_only_is_partial() -> None:
    geom, status = parse_shape("HAZARD AT 12-34N 113-45E")
    assert status == "partial" and geom and geom["type"] == "Point"
    geom, status = parse_shape("A 12-34N 113-45E B 12-40N 113-50E")
    assert status == "partial" and geom and geom["type"] == "MultiPoint"


@pytest.mark.parametrize("text", ["", "no coordinates here", "12-34N", "\x00\x01 ~~~", "北纬"])
def test_malformed_is_no_geometry(text: str) -> None:
    assert parse_shape(text) == (None, "no_geometry")


def test_beijing_time_converted_to_utc() -> None:
    v = parse_validity("一、2026年9月30日0800时至1800时，在下列")
    assert v.valid_from == datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    assert v.valid_to == datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    v2 = parse_validity("2026年9月29日0600时至2026年9月29日1400时")
    assert v2.valid_from == datetime(2026, 9, 28, 22, 0, tzinfo=UTC)  # day rolls back in UTC


def test_dtg_and_month_year_rollover() -> None:
    ref = datetime(2026, 12, 30, tzinfo=UTC)
    assert parse_dtg("301200Z DEC 26") == datetime(2026, 12, 30, 12, tzinfo=UTC)
    v = parse_validity("312000Z TO 010400Z", ref)  # no month: end rolls into January 2027
    assert v.valid_from == datetime(2026, 12, 31, 20, tzinfo=UTC)
    assert v.valid_to == datetime(2027, 1, 1, 4, tzinfo=UTC)
    v = parse_validity("312000Z DEC 26 TO 010400Z JAN 27")
    assert v.valid_to == datetime(2027, 1, 1, 4, tzinfo=UTC)
    v = parse_validity("DAILY 301200Z SEP TO 021200Z OCT", datetime(2026, 9, 30, tzinfo=UTC))
    assert v.valid_to == datetime(2026, 10, 2, 12, tzinfo=UTC)


def test_cancel_this_msg() -> None:
    v = parse_validity("HAZARD. CANCEL THIS MSG 281200Z SEP 26.")
    assert v.cancelled and v.valid_to == datetime(2026, 9, 28, 12, tzinfo=UTC)


def test_notam_validity() -> None:
    v = parse_validity("A) VYYF B) 2609300800 C) 2609301800")
    assert v.valid_from == datetime(2026, 9, 30, 8, tzinfo=UTC)
    assert v.valid_to == datetime(2026, 9, 30, 18, tzinfo=UTC) and not v.permanent
    v = parse_validity("B) 2609200100 C) PERM")
    assert v.permanent and v.valid_to is None
    v = parse_validity("B) 2609300000 C) 2610070000 EST")
    assert v.estimated and v.valid_to == datetime(2026, 10, 7, tzinfo=UTC)
    assert parse_validity("B) 2613999999").valid_from is None  # invalid date -> None, no raise


def test_categories_en_zh() -> None:
    assert categorize("GPS UNRELIABLE WITHIN 100NM", RULES) == "gnss_interference"
    assert categorize("Live Firing exercise", RULES) == "live_firing"
    assert categorize("南海 军事演习", RULES) == "military_exercise"
    assert categorize("实弹射击 军事演习", RULES) == "live_firing"  # rules order wins
    assert categorize("cargo report", RULES) == "other"


def test_sanitize_html_and_controls() -> None:
    out = sanitize(
        "<p>Hi<script>alert(1)</script><b>there</b>\x00\x07</p><style>x{}</style>ＡＢＣ１２３",
        html=True,
    )
    assert "alert" not in out and "<" not in out and "\x00" not in out
    assert "ABC123" in out and "Hi" in out
    assert len(sanitize("x" * 50000)) == 8000
    assert sanitize("a\nb\x0bc") == "a\nbc"


def test_make_ref() -> None:
    r = make_ref("HYDROPAC", "1234/26", "<script>x")
    assert r == "HYDROPAC 1234/26 SCRIPTX" and len(make_ref("A" * 100)) == 40
    assert make_ref("ab;cd") == "ABCD"


def test_covering_cells() -> None:
    poly = circle_polygon(12.5, 113.5, 10 * 1.852)
    cells = covering_cells(poly)
    assert cells
    assert covering_cells({"type": "Point", "coordinates": [113.5, 12.5]}) and len(cells) < 50
    huge = {
        "type": "Polygon",
        "coordinates": [[[90, -10], [150, -10], [150, 40], [90, 40], [90, -10]]],
    }
    assert covering_cells(huge) is None


def test_pathological_input_is_fast() -> None:
    nasty = "1234" * 25000 + "N" + "9" * 5000 + "12-" * 20000 + "北纬" * 20000
    t0 = time.perf_counter()
    parse_coords(nasty)
    parse_shape(nasty)
    parse_validity(nasty)
    assert time.perf_counter() - t0 < 1.0
