"""IODA raw signals -> outage_signal GeoEvents (hand-built fixture, respx, offline)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import respx

from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.ioda import IodaConnector, IodaEntity, load_entities, parse_ioda_signals

FIX = Path(__file__).parent.parent / "fixtures" / "ioda"
BASE = "https://ioda.example.invalid/v2"
MM = IodaEntity("country", "MM", "Myanmar", 21.9, 95.96, "myanmar")


def _payload() -> dict[str, Any]:
    return json.loads((FIX / "signals_mm.json").read_text())


def _by_ds(events: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for e in sorted(events, key=lambda x: x.ts):
        out.setdefault(e.attrs["datasource"], []).append(e)
    return out


def test_drop_and_flat_series() -> None:
    events, dropped = parse_ioda_signals(_payload(), MM, baseline_h=168)
    assert dropped == 0
    by = _by_ds(events)
    assert set(by) == {"bgp", "ping-slash24", "merit-nt"}
    for ds in ("bgp", "ping-slash24"):
        assert all(e.value < 0.5 for e in by[ds][-6:])  # the 6 drop bins
        assert all(0.9 < e.value < 1.1 for e in by[ds][:-6])
    assert all(0.98 < e.value < 1.02 for e in by["merit-nt"])  # flat control
    last = by["bgp"][-1]
    assert last.attrs["observed"] == 200 and last.attrs["baseline"] == 1000
    assert last.kind == "outage_signal" and last.source == "ioda" and last.domain.value == "cyber"
    assert last.attrs["role"] == "signal" and last.attrs["entity_code"] == "MM"
    assert last.attrs["level"] == "country" and last.lat == 21.9 and last.h3_r4


def test_null_points_skipped_and_history_required() -> None:
    events, _ = parse_ioda_signals(_payload(), MM, baseline_h=168)
    by = _by_ds(events)
    # 200 points; the first 24 lack a baseline. ping has 4 null bins too.
    assert len(by["bgp"]) == 200 - 24
    assert len(by["ping-slash24"]) == 200 - 24 - 4


def test_ids_stable_and_emit_window() -> None:
    a = parse_ioda_signals(_payload(), MM)[0]
    b = parse_ioda_signals(_payload(), MM)[0]
    assert [e.id for e in a] == [e.id for e in b] and len({e.id for e in a}) == len(a)
    recent, _ = parse_ioda_signals(_payload(), MM, emit_h=3)
    assert len(recent) == 9  # 3 hourly bins x 3 datasources
    assert {e.id for e in recent} <= {e.id for e in a}


def test_bad_series_dropped() -> None:
    p = {"data": [[{"datasource": "bgp", "from": 0, "step": 0, "values": []}, "junk"]]}
    assert parse_ioda_signals(p, MM) == ([], 2)
    neg = {"data": [[{"from": 0, "step": 3600, "values": [5.0] * 30 + [-1.0]}]]}
    events, dropped = parse_ioda_signals(neg, MM, datasource="bgp")
    assert dropped == 1 and len(events) == 6  # bins 24..29 have baselines


def test_entities_config() -> None:
    ents = {e.code: e for e in load_entities()}
    assert {"MM", "PH", "TW", "VN", "MY", "SG", "ID", "CN", "EE", "LV", "LT"} <= set(ents)
    assert ents["MM"].aoi == "myanmar" and -90 < ents["MM"].lat < 90


@respx.mock
async def test_poll_requests_each_entity_datasource() -> None:
    route = respx.get(url__regex=rf"{BASE}/signals/raw/country/MM").mock(
        return_value=httpx.Response(200, json=_payload())
    )
    c = IodaConnector(
        "ioda",
        BASE,
        [MM],
        client=RateLimitedClient("ioda", rps=1000, max_retries=0),
        now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
        emit_h=2,
    )
    events = await c.poll_once()
    assert route.call_count == 3
    assert {r.request.url.params["datasource"] for r in route.calls} == {
        "bgp",
        "ping-slash24",
        "merit-nt",
    }
    assert len(events) == 6  # same series returned for each ds; ids include the requested ds
    assert c.health().status == "ok"  # keyless: never needs_key
