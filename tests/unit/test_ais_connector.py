"""AisStreamConnector with a fake WebSocket: subscription, thinning, reconnect, error frames."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from websockets.exceptions import ConnectionClosedError

from heimdall.config import AOI
from heimdall.geo import bbox_for_radius
from heimdall.ingest.ais import AisStreamConnector
from heimdall.schemas import VesselObs

FIX = Path(__file__).parent.parent / "fixtures" / "aisstream" / "stream_sample.jsonl"
KEY = "sekrit-key-1234"
AOIS = [
    AOI(name="malacca", lat=3.0, lon=100.8, radius_nm=250),
    AOI(name="scs", lat=12.5, lon=113.5, radius_nm=250),
    AOI(name="fiji", lat=-17.0, lon=179.5, radius_nm=250),  # crosses the antimeridian
]


class FakeWS:
    def __init__(self, frames: list[str]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if not self.frames:
            raise ConnectionClosedError(None, None)
        return self.frames.pop(0)


class FakeConnect:
    """Factory: each call hands out the next scripted session (list of frames)."""

    def __init__(self, sessions: list[list[str]]) -> None:
        self.sessions = list(sessions)
        self.sockets: list[FakeWS] = []
        self.urls: list[str] = []

    def __call__(self, url: str) -> FakeConnect:
        self.urls.append(url)
        self._ws = FakeWS(self.sessions.pop(0) if self.sessions else [])
        self.sockets.append(self._ws)
        return self

    async def __aenter__(self) -> FakeWS:
        return self._ws

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _make(
    sessions: list[list[str]], max_sleeps: int = 1, **kw: Any
) -> tuple[AisStreamConnector, FakeConnect]:
    fc = FakeConnect(sessions)
    holder: dict[str, AisStreamConnector] = {}

    async def fake_sleep(delay: float) -> None:
        if len(holder["c"].sleeps) >= max_sleeps:
            holder["c"]._stop = True  # end the reconnect loop

    c = AisStreamConnector(
        "aisstream",
        "wss://stream.example.invalid/v0/stream",
        AOIS,
        KEY,
        connect=fc,
        sleep=fake_sleep,
        jitter=lambda: 1.0,
        **kw,
    )
    holder["c"] = c
    return c, fc


async def _drain(c: AisStreamConnector) -> list[BaseModel]:
    out: list[BaseModel] = []
    async for batch in c.stream():
        out.extend(batch)
    return out


def _frames() -> list[str]:
    return [x for x in FIX.read_text().splitlines() if x.strip()]


async def test_replays_fixture_with_thinning_and_drop_counts() -> None:
    c, _ = _make([_frames()])
    rows = await _drain(c)
    assert all(isinstance(r, VesselObs) for r in rows)
    assert len(rows) == 15  # 16 valid positions minus one thinned repeat within 10 s
    assert c.n_thinned == 1
    assert c.n_dropped == 6  # 3 bad coordinates, 2 bad MMSI, 1 empty body
    assert c.records_hour.total() == 15


async def test_first_frame_is_subscription_with_bbox_per_aoi() -> None:
    c, fc = _make([_frames()[:3]])
    await _drain(c)
    ws = fc.sockets[0]
    sub = json.loads(ws.sent[0])
    assert sub["APIKey"] == KEY
    assert sub["FilterMessageTypes"] and "PositionReport" in sub["FilterMessageTypes"]
    # one box per AOI; the antimeridian one is split in two
    assert len(sub["BoundingBoxes"]) == 2 + len(bbox_for_radius(-17.0, 179.5, 250))
    for (lat1, lon1), (lat2, lon2) in sub["BoundingBoxes"]:
        assert lat1 < lat2 and lon1 < lon2
    assert len(ws.sent) == 1
    assert fc.urls == ["wss://stream.example.invalid/v0/stream"]


async def test_drop_reconnects_with_exponential_backoff() -> None:
    c, fc = _make([[], [], [], [], []], max_sleeps=4)
    await _drain(c)
    assert c.sleeps == [1.0, 2.0, 4.0, 8.0]
    assert len(fc.sockets) == 4  # reconnected each time


async def test_backoff_caps_and_resets_after_healthy_traffic() -> None:
    c, _ = _make([])
    assert c._delay(30) == 300.0
    good = _frames()[3]  # a valid PositionReport
    c2, _ = _make([[], [], [good], []], max_sleeps=4)
    await _drain(c2)
    assert c2.sleeps == [1.0, 2.0, 1.0, 2.0]  # traffic on the third session reset the ladder


async def test_error_frame_marks_degraded_and_never_leaks_key(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    err = json.dumps({"error": f"Api Key Is Not Valid: {KEY}"})
    c, _ = _make([[err]])
    assert await _drain(c) == []
    h = c.health()
    assert h.status == "degraded"
    assert h.detail and "Api Key Is Not Valid" in h.detail
    blob = h.model_dump_json() + caplog.text + capsys.readouterr().out
    assert KEY not in blob
    assert "***" in (h.detail or "")


async def test_recovers_to_ok_after_good_batch() -> None:
    err = json.dumps({"error": "boom"})
    c, _ = _make([[err], [_frames()[3]]], max_sleeps=2)
    await _drain(c)
    assert c.n_err >= 1
    assert c.health().status == "ok"


async def test_health_reports_ok_detail_without_key() -> None:
    c, _ = _make([[_frames()[3]]])
    await _drain(c)
    h = c.health()
    assert h.status == "ok" and KEY not in h.model_dump_json()
    assert h.records_last_hour == 1


async def test_bad_json_frame_counted_not_fatal() -> None:
    c, _ = _make([["not json", _frames()[3]]])
    rows = await _drain(c)
    assert len(rows) == 1 and c.n_dropped == 1
