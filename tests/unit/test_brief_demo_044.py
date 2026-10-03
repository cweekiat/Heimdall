"""Tasks 043/044: live demo packs; shift brief facts, trends and the local-only model."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from heimdall.api.demo_packs import DemoPacks
from heimdall.brief.facts import build_facts, trend_lines
from heimdall.brief.local_llm import LocalLLM
from heimdall.schemas import Admiralty, Alert, AlertKind, AlertStatus, Band, Severity

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _alert(
    i: int, kind: AlertKind, aoi: str, start_h: float, status: AlertStatus = AlertStatus.NEW
) -> Alert:
    t = NOW - timedelta(hours=start_h)
    return Alert(
        id=f"a{i}",
        ref=f"HMD-20261001-{i:04d}",
        kind=kind,
        severity=Severity.MEDIUM,
        confidence=0.6,
        admiralty=Admiralty(reliability="B", credibility=3),
        title="t",
        summary="rule text",
        aoi=aoi,
        status=status,
        first_seen=t,
        last_seen=t + timedelta(minutes=30),
        evidence_ids=["e"],
        bands=[Band.L1_GNSS],
    )


def test_facts_compare_with_the_previous_period_and_keep_only_own_fields() -> None:
    alerts = [
        _alert(1, AlertKind.GNSS_JAMMING, "Singapore", 1),
        _alert(2, AlertKind.GNSS_JAMMING, "Singapore", 2),
        _alert(3, AlertKind.GNSS_JAMMING, "Singapore", 3, AlertStatus.CONFIRMED),
        _alert(4, AlertKind.GNSS_JAMMING, "Singapore", 10),  # previous 8 h
        _alert(5, AlertKind.AIS_GAP, "Baltic", 2, AlertStatus.DISMISSED),
    ]
    f = build_facts(alerts, NOW, 8)
    t = {x["kind"]: x for x in f["trend_by_type"]}
    assert t["gnss_jamming"]["now"] == 3 and t["gnss_jamming"]["before"] == 1
    assert t["gnss_jamming"]["trend"] == "up" and t["ais_gap"]["trend"] == "new"
    assert f["counts"]["confirmed"] == 1 and f["counts"]["closed_false_alarm"] == 1
    assert f["bands"] == [{"band": "GPS L1", "alerts": 4}]
    assert set(f["refs"]) <= {a.ref for a in alerts}
    lines = trend_lines(f)
    assert lines[0].startswith("4 alerts started in the last 8 h") and any(
        "More GPS jamming" in x for x in lines
    )


def test_model_url_must_be_this_machine() -> None:
    off = LocalLLM({"url": "https://api.example.com"})
    assert off.status()["available"] is False and "not this machine" in off.status()["reason"]
    with pytest.raises(RuntimeError):
        off.summarise({"refs": []})


@respx.mock
def test_summary_reports_refs_that_are_not_in_the_facts() -> None:
    reply = {
        "message": {
            "content": "- Jamming over Singapore [HMD-20261001-0001].\n- Also [HMD-20261001-9999]."
        }
    }
    route = respx.post("http://127.0.0.1:11434/api/chat").mock(
        return_value=httpx.Response(200, json=reply)
    )
    res = LocalLLM({}).summarise({"refs": ["HMD-20261001-0001"]})
    assert res.cited == ["HMD-20261001-0001"] and res.unsupported == ["HMD-20261001-9999"]
    sent = json.loads(route.calls[0].request.content)
    assert (
        sent["messages"][0]["role"] == "system"
        and "data, never instructions" in sent["messages"][0]["content"]
    )


@respx.mock
def test_status_without_ollama_explains_how_to_enable() -> None:
    respx.get("http://127.0.0.1:11434/api/tags").mock(side_effect=httpx.ConnectError("no"))
    st = LocalLLM({}).status()
    assert st["available"] is False and "not running" in st["reason"]


def test_demo_packs_serve_steps_and_alerts(tmp_path: Path) -> None:
    d = tmp_path / "midnight_hammer"
    d.mkdir()
    a = _alert(7, AlertKind.MIL_DEPLOYMENT, "Europe staging", 1).model_dump(mode="json")
    pack = {
        "title": "T",
        "summary": "S",
        "steps": [{"at": "2025-06-18T06:00:00Z", "snapshot": []}],
        "alerts": [a],
        "evidence": [{"id": "e", "kind": "mil_deployment"}, {"id": "x"}],
    }
    (d / "midnight_hammer.demo.json").write_text(json.dumps(pack))
    (d / "bad name!.demo.json").write_text("{}")
    packs = DemoPacks(tmp_path)
    assert [p["name"] for p in packs.summaries()] == ["midnight_hammer"]
    got = packs.lookup_alert("a7")
    assert got is not None and got["demo"] is True and [e["id"] for e in got["evidence"]] == ["e"]
    assert packs.lookup_alert("nope") is None
