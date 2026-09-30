"""The pipeline hands weather/Kp/footprint/overpass events to fusion as context (task 022a/b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from heimdall.pipeline import _p3_windows


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[datetime, datetime, list[str]]] = []

    def events_window(self, start: datetime, end: datetime, kinds: list[str]) -> list[object]:
        self.calls.append((start, end, list(kinds)))
        return [f"ev:{k}" for k in kinds]


def test_context_window_covers_confounders_and_future_overpasses() -> None:
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    st = _Store()
    w = _p3_windows(st, {"aoi": {}}, now)  # type: ignore[arg-type]
    start, end, kinds = next(c for c in st.calls if "weather" in c[2])
    assert set(kinds) == {"weather", "space_weather", "imagery_footprint", "overpass"}
    assert start <= now - timedelta(hours=24) and end >= now + timedelta(hours=24)
    assert len(w.context) == 4
