"""bbox_for_radius: plain, antimeridian and polar cases."""

from __future__ import annotations

import pytest

from heimdall.geo import bbox_for_radius


def test_plain_box_contains_circle_extent() -> None:
    ((s, w, n, e),) = bbox_for_radius(3.0, 100.8, 250)
    assert s == pytest.approx(3.0 - 250 / 60, abs=0.05)
    assert n == pytest.approx(3.0 + 250 / 60, abs=0.05)
    assert w < 100.8 < e and (e - w) / 2 == pytest.approx(250 / 60, rel=0.01)


def test_antimeridian_splits_into_two_boxes() -> None:
    boxes = bbox_for_radius(-17.0, 179.5, 250)
    assert len(boxes) == 2
    assert boxes[0][3] == 180.0 and boxes[1][1] == -180.0
    assert boxes[0][1] < 180.0 and boxes[1][3] > -180.0
    west_side = bbox_for_radius(-17.0, -179.5, 250)
    assert len(west_side) == 2 and west_side[0][3] == 180.0


def test_pole_gives_full_longitude_band() -> None:
    ((_, w, n, e),) = bbox_for_radius(89.0, 10.0, 250)
    assert (w, e, n) == (-180.0, 180.0, 90.0)
