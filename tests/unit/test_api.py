import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.api.demo import seed

PB = {
    "actions": {
        "watch_cell": {"label": "w", "risk": "low", "applies_to": ["gnss_jamming"]},
        "export_brief": {"label": "e", "risk": "low", "applies_to": ["*"]},
    }
}


@pytest.fixture
def client() -> TestClient:
    store = seed(MemoryStore())
    return TestClient(create_app(store, playbook=PB))


def test_health_aois_playbook(client: TestClient) -> None:
    assert client.get("/api/health").json()["sources"]
    assert len(client.get("/api/aois").json()) >= 5
    assert {a["id"] for a in client.get("/api/playbook").json()} == set(PB["actions"])


def test_alert_list_filters_and_detail(client: TestClient) -> None:
    allr = client.get("/api/alerts").json()
    assert len(allr) == 8
    assert all(a["aoi"] == "baltic" for a in client.get("/api/alerts?aoi=baltic").json())
    assert client.get("/api/alerts?status=dismissed").json() == []
    assert client.get("/api/alerts?status=bogus").status_code == 422
    d = client.get("/api/alerts/alert-0").json()
    assert d["evidence"][0]["rules"] and d["alert"]["admiralty"]
    ev = client.get(f"/api/evidence/{d['evidence'][0]['id']}")
    assert ev.status_code == 200
    assert client.get("/api/alerts/nope").status_code == 404
    assert client.get("/api/evidence/nope").status_code == 404
    assert client.get("/api/alerts?since=2000-01-01T00:00:00Z").status_code == 200


def test_cells_and_tracks(client: TestClient) -> None:
    cells = client.get("/api/cells?res=4").json()
    assert cells and "ratio" in cells[0]
    tr = client.get("/api/tracks/7C6B00").json()
    assert len(tr) > 10
    assert client.get("/api/tracks/ffffff").json() == []


def test_metrics(client: TestClient) -> None:
    cell = client.get("/api/alerts/alert-0").json()["alert"]["h3_cell"]
    pts = client.get(f"/api/metrics/{cell}/degraded_ratio").json()
    assert len(pts) >= 20 and "value" in pts[0]


def test_feedback(client: TestClient) -> None:
    r = client.post("/api/alerts/alert-0/feedback", json={"verdict": "dismiss", "reason": "dup"})
    assert r.status_code == 200
    d = client.get("/api/alerts/alert-0").json()
    assert d["alert"]["status"] == "dismissed" and d["feedback"][0]["reason"] == "dup"
    assert client.post("/api/alerts/alert-0/feedback", json={"verdict": "maybe"}).status_code == 422


def test_actions_playbook_validation(client: TestClient) -> None:
    bad = client.post("/api/alerts/alert-0/actions", json={"action_id": "rm -rf /"})
    assert bad.status_code == 422
    # watch_cell does not apply to mil_orbit (alert-3)
    assert (
        client.post("/api/alerts/alert-3/actions", json={"action_id": "watch_cell"}).status_code
        == 422
    )
    ok = client.post("/api/alerts/alert-0/actions", json={"action_id": "watch_cell"})
    assert ok.status_code == 200 and ok.json()["status"] == "proposed"
    aid = ok.json()["id"]
    ap = client.post(f"/api/actions/{aid}/approve")
    assert ap.status_code == 200 and ap.json()["result"]["executed"] is False
    assert client.post(f"/api/actions/{aid}/approve").status_code == 404
    ok2 = client.post("/api/alerts/alert-0/actions", json={"action_id": "export_brief"}).json()
    assert client.post(f"/api/actions/{ok2['id']}/reject").json()["status"] == "rejected"
    assert (
        client.post("/api/alerts/nope/actions", json={"action_id": "watch_cell"}).status_code == 404
    )


def test_stream_emits_updates() -> None:
    store = seed(MemoryStore())
    app = create_app(store, playbook=PB)

    async def run() -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        gen = app.state.alert_events(interval=0.01, max_ticks=100)
        async for ev in gen:
            out.append(ev)
            break
        return out

    async def go() -> list[dict[str, str]]:
        task = asyncio.create_task(run())
        await asyncio.sleep(0.03)
        a = store.get_alert("alert-1")
        assert a is not None
        a.last_seen = datetime.now(UTC)
        store.upsert_alert(a)
        return await asyncio.wait_for(task, 2)

    events = asyncio.run(go())
    assert events and events[0]["event"] == "alert"
