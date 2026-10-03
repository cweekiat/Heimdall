"""054: affected aircraft in alert details carry callsign / type / role (not just the hex)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.fuse.fusion import fuse
from heimdall.schemas import AircraftObs, AlertKind
from tests.unit.test_fusion import GRADES, PB, ev


def test_alert_detail_entity_info() -> None:
    now = datetime.now(UTC)
    s = MemoryStore()
    s.insert_aircraft(
        [
            AircraftObs(
                ts=now - timedelta(minutes=5),
                hex="ae04c5",
                provider="adsb.lol",
                lat=26.0,
                lon=51.0,
                flight="QID21  ",
                type_code="K35R",
                mil=True,
                src_type="adsb_icao",
            )
        ]
    )
    e = ev(kind=AlertKind.MIL_DEPLOYMENT, entities=["ae04c5", "abc123"])
    e = e.model_copy(update={"window_end": now, "window_start": now - timedelta(minutes=30)})
    s.upsert_evidence(e)
    (a,) = fuse([e], [], now, None, PB, GRADES)
    s.upsert_alert(a)
    d = TestClient(create_app(s, allowed_hosts=["testserver"])).get(f"/api/alerts/{a.id}").json()
    info = d["entity_info"]
    assert info["ae04c5"]["flight"] == "QID21" and info["ae04c5"]["role"] == "tanker"
    assert info["ae04c5"]["type"] == "K35R" and info["ae04c5"]["mil"] is True
    assert info["abc123"]["flight"] is None and info["abc123"]["role"] is None
