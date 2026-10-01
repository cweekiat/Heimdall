from __future__ import annotations

from heimdall.api.basemap import build_style


def test_region_only_style() -> None:
    st = build_style("dark")
    assert list(st["sources"]) == ["protomaps"]
    ids = [layer["id"] for layer in st["layers"]]
    assert ids[0] == "background" and "earth" in ids and "water" in ids


def test_overview_drawn_under_region_without_duplicate_labels() -> None:
    st = build_style("dark", overview=True)
    assert set(st["sources"]) == {"protomaps", "world"}
    ids = [layer["id"] for layer in st["layers"]]
    assert ids.index("earth-world") < ids.index("earth")  # world first, region on top
    places = next(layer for layer in st["layers"] if layer["id"] == "places-world")
    assert places["filter"][0] == "!" and places["filter"][1][0] == "within"
