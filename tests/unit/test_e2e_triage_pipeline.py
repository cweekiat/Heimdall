"""End-to-end: live-path store (CompositeStore over DuckDB, ADR-002) + pipeline ticks + API triage.

An analyst's triage decision must survive later pipeline updates, and no duplicate alert may appear.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heimdall.adapters.composite_store import CompositeStore
from heimdall.adapters.duckdb_store import DuckDBStore
from heimdall.api.app import create_app
from heimdall.pipeline import tick
from heimdall.schemas import AlertKind
from tests.synthetic.generators import T0, jammed_cell
from tests.unit.test_fusion import GRADES, PB, _settings

HOSTS = ["testserver", "127.0.0.1", "localhost"]


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        ("acknowledged", {}),
        ("under_review", {}),
        ("confirmed", {}),
        ("dismissed", {"reason": "known exercise"}),
    ],
)
def test_triage_survives_pipeline_ticks(tmp_path: Path, status: str, extra: dict[str, str]) -> None:
    store = CompositeStore(DuckDBStore(tmp_path / "h.duckdb"), DuckDBStore(tmp_path / "a.duckdb"))
    store.insert_aircraft(jammed_cell(n_aircraft=30, n_jammed=10, minutes=45))
    cfg = _settings()
    for m in (35, 40):
        tick(store, cfg, T0 + timedelta(minutes=m), playbook=PB, source_grades=GRADES)
    jams = [a for a in store.list_alerts() if a.kind == AlertKind.GNSS_JAMMING]
    assert len(jams) == 1
    alert = jams[0]

    client = TestClient(create_app(store, playbook=PB, allowed_hosts=HOSTS))
    r = client.post(f"/api/alerts/{alert.id}/status", json={"status": status, **extra})
    assert r.status_code == 200, r.text

    tick(store, cfg, T0 + timedelta(minutes=45), playbook=PB, source_grades=GRADES)

    body = client.get(f"/api/alerts/{alert.id}").json()
    assert body["alert"]["status"] == status
    after = [a for a in store.list_alerts() if a.kind == AlertKind.GNSS_JAMMING]
    # still the same single alert, refreshed by the pipeline (dismissed owns its episode too)
    assert [a.id for a in after] == [alert.id]
    assert after[0].last_seen > alert.last_seen
    assert any(n["status_to"] == status for n in body["notes"])
