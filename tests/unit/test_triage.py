from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heimdall.adapters.composite_store import CompositeStore
from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.adapters.memory_store import MemoryStore
from heimdall.api import triage
from heimdall.api.app import create_app
from heimdall.api.demo import seed
from heimdall.schemas import Alert, AlertStatus

HOSTS = ["testserver", "127.0.0.1", "localhost"]
PB = {"actions": {"export_brief": {"label": "e", "risk": "low", "applies_to": ["*"]}}}
S = AlertStatus


@pytest.fixture
def store() -> MemoryStore:
    s = MemoryStore()
    seed(s)
    return s


@pytest.fixture
def client(store: MemoryStore) -> TestClient:
    return TestClient(create_app(store, playbook=PB, allowed_hosts=HOSTS))


def _future(h: int = 2) -> str:
    return (datetime.now(UTC) + timedelta(hours=h)).isoformat()


def test_transition_table() -> None:
    now = datetime.now(UTC)
    assert triage.validate(S.NEW, S.ACKNOWLEDGED, None, None, now) is None
    assert triage.validate(S.NEW, S.EXPIRED, None, None, now)  # pipeline-owned
    assert triage.validate(S.EXPIRED, S.ACKNOWLEDGED, None, None, now)
    assert triage.validate(S.CONFIRMED, S.DISMISSED, "x", None, now)  # must reopen first
    assert triage.validate(S.CONFIRMED, S.UNDER_REVIEW, None, None, now) is None
    assert triage.validate(S.NEW, S.DISMISSED, " ", None, now)  # reason required
    assert triage.validate(S.NEW, S.SHELVED, "x", None, now)  # until required
    assert triage.validate(S.NEW, S.SHELVED, "x", now - timedelta(1), now)  # in the future
    assert triage.validate(S.NEW, S.SHELVED, "x", now + timedelta(1), now) is None
    assert triage.validate(S.NEW, S.ACKNOWLEDGED, None, now + timedelta(1), now)


def test_status_endpoint_writes_journal(client: TestClient) -> None:
    r = client.post("/api/alerts/alert-0/status", json={"status": "acknowledged"})
    assert r.status_code == 200 and r.json()["alert"]["status"] == "acknowledged"
    no_reason = client.post("/api/alerts/alert-0/status", json={"status": "dismissed"})
    assert no_reason.status_code == 422
    ok = client.post("/api/alerts/alert-0/status", json={"status": "dismissed", "reason": "dup"})
    assert ok.status_code == 200
    bad = client.post("/api/alerts/alert-0/status", json={"status": "acknowledged"})
    assert bad.status_code == 422  # dismissed -> acknowledged not allowed
    notes = client.get("/api/alerts/alert-0/notes").json()
    assert [n["status_to"] for n in notes] == ["acknowledged", "dismissed"]
    assert notes[1]["text"] == "dup" and notes[1]["kind"] == "status"
    assert client.get("/api/alerts/alert-0").json()["notes"] == notes
    missing = client.post("/api/alerts/nope/status", json={"status": "acknowledged"})
    assert missing.status_code == 404
    assert client.post("/api/alerts/alert-1/status", json={"status": "new"}).status_code == 422


def test_shelve_and_notes(client: TestClient) -> None:
    body = {"status": "shelved", "reason": "known exercise"}
    assert client.post("/api/alerts/alert-2/status", json=body).status_code == 422
    body["shelve_until"] = _future()
    r = client.post("/api/alerts/alert-2/status", json=body)
    assert r.status_code == 200 and r.json()["note"]["kind"] == "shelve"
    n = client.post("/api/alerts/alert-2/notes", json={"text": "<b>watch</b> this"})
    assert n.status_code == 200 and n.json()["text"] == "<b>watch</b> this"  # stored verbatim
    assert client.post("/api/alerts/alert-2/notes", json={"text": ""}).status_code == 422
    assert client.post("/api/alerts/alert-2/notes", json={"text": "x" * 2001}).status_code == 422
    assert len(client.get("/api/alerts/alert-2/notes").json()) == 2


def test_pipeline_upsert_does_not_overwrite_analyst_status(
    store: MemoryStore, client: TestClient
) -> None:
    client.post("/api/alerts/alert-0/status", json={"status": "acknowledged"})
    a: Alert = store.alerts["alert-0"]
    store.upsert_alert(a.model_copy(update={"status": S.NEW, "last_seen": datetime.now(UTC)}))
    assert client.get("/api/alerts/alert-0").json()["alert"]["status"] == "acknowledged"


def test_composite_overlay_notes_and_shelve_expiry(tmp_path: Path) -> None:
    main_path = tmp_path / "m.duckdb"
    m = DuckDBStore(main_path)
    seed(m)
    m.close()
    main = DuckDBStore(main_path, read_only=True)
    analyst = DuckDBStore(tmp_path / "a.duckdb")
    comp = CompositeStore(main, analyst)
    c = TestClient(create_app(comp, playbook=PB, allowed_hosts=HOSTS))
    assert c.post("/api/alerts/alert-3/status", json={"status": "under_review"}).status_code == 200
    # the pipeline's stored alert stays NEW; the API overlays the analyst status
    main_alert = main.get_alert("alert-3")
    assert main_alert is not None and main_alert.status == S.NEW
    assert c.get("/api/alerts/alert-3").json()["alert"]["status"] == "under_review"
    restarted = CompositeStore(main, analyst).get_alert("alert-3")
    assert restarted is not None and restarted.status == S.UNDER_REVIEW
    # shelve via the API with a short expiry; a patched clock passes it
    r = c.post(
        "/api/alerts/alert-4/status",
        json={"status": "shelved", "reason": "r", "shelve_until": _future(1)},
    )
    assert r.status_code == 200
    assert c.get("/api/alerts/alert-4").json()["alert"]["status"] == "shelved"
    # a pipeline upsert must not cancel the shelve
    a4 = main.get_alert("alert-4")
    assert a4 is not None
    main.upsert_alert(a4.model_copy(update={"status": S.NEW}))  # pipeline write path
    assert c.get("/api/alerts/alert-4").json()["alert"]["status"] == "shelved"
    assert comp._shelved_until.get("alert-4") is not None
    later = datetime.now(UTC) + timedelta(hours=2)
    comp._now = lambda: later
    assert c.get("/api/alerts/alert-4").json()["alert"]["status"] == "acknowledged"
    notes = c.get("/api/alerts/alert-4/notes").json()
    assert notes[-1]["user"] == "system" and notes[-1]["status_to"] == "acknowledged"
    # the expiry survives a restart (journalled)
    again = CompositeStore(main, analyst, clock=lambda: later).get_alert("alert-4")
    assert again is not None and again.status == S.ACKNOWLEDGED


def test_live_composite_pipeline_upsert_keeps_ack(tmp_path: Path) -> None:
    main = DuckDBStore(tmp_path / "m.duckdb")
    seed(main)
    comp = CompositeStore(main, DuckDBStore(tmp_path / "a.duckdb"))
    c = TestClient(create_app(comp, playbook=PB, allowed_hosts=HOSTS))
    assert c.post("/api/alerts/alert-3/status", json={"status": "acknowledged"}).status_code == 200
    a = main.get_alert("alert-3")
    assert a is not None
    comp.upsert_alert(a.model_copy(update={"status": S.NEW, "last_seen": datetime.now(UTC)}))
    assert c.get("/api/alerts/alert-3").json()["alert"]["status"] == "acknowledged"


def test_feedback_fallback_when_no_status_note(tmp_path: Path) -> None:
    from heimdall.schemas import Feedback

    main = DuckDBStore(tmp_path / "m.duckdb")
    seed(main)
    analyst = DuckDBStore(tmp_path / "a.duckdb")
    analyst.add_feedback(
        Feedback(alert_id="alert-3", verdict="confirm", reason="x", ts=datetime.now(UTC))
    )
    got = CompositeStore(main, analyst).get_alert("alert-3")
    assert got is not None and got.status == S.CONFIRMED


def test_feedback_reason_max_length(client: TestClient) -> None:
    r = client.post(
        "/api/alerts/alert-1/feedback", json={"verdict": "dismiss", "reason": "x" * 1001}
    )
    assert r.status_code == 422


def test_aoi_summary(client: TestClient) -> None:
    rows = {r["name"]: r for r in client.get("/api/aois/summary").json()}
    scs = rows["south_china_sea"]
    assert scs["label"] == "South China Sea"
    assert scs["open_by_kind"] == {"gnss_jamming": 1, "mil_orbit": 1}
    assert scs["max_severity"] == "critical" and scs["ratio_now"] == pytest.approx(0.58)
    assert len(scs["spark"]) == 24 and scs["ratio_median"] is not None
    assert scs["coverage"] > 0 and scs["last_change"]
    client.post("/api/alerts/alert-2/status", json={"status": "dismissed", "reason": "x"})
    rows = {r["name"]: r for r in client.get("/api/aois/summary").json()}
    assert rows["south_china_sea"]["open_by_kind"] == {"mil_orbit": 1}


def test_no_override_is_cached_and_invalidated_by_feedback() -> None:
    from datetime import UTC, datetime

    from heimdall.adapters.composite_store import CompositeStore
    from heimdall.adapters.memory_store import MemoryStore
    from heimdall.schemas import Feedback

    main, analyst = MemoryStore(), MemoryStore()
    comp = CompositeStore(main, analyst)
    calls = {"n": 0}
    orig = analyst.notes_for

    def counting(alert_id: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return orig(alert_id)

    analyst.notes_for = counting  # type: ignore[method-assign]
    assert comp._override("a1") is None
    assert comp._override("a1") is None
    assert calls["n"] == 1  # second read served from the no-override cache
    comp.add_feedback(Feedback(alert_id="a1", verdict="dismiss", ts=datetime.now(UTC)))
    assert comp._override("a1") is not None
