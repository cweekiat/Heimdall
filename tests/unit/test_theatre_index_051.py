"""051: theatre activity index - explainable components, level = count above normal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from heimdall import theatre_index as ti
from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.detect import military as mil
from heimdall.schemas import Domain, GeoEvent

TH = {"name": "Gulf", "lat": 26.0, "lon": 51.5, "radius_km": 900}


def test_quiet_learning_and_counted_levels() -> None:
    q = ti.build(TH, 4, [4.0] * 100, [], [], [], 2, [2, 3, 2], 0)
    assert q.level == 0 and q.word == "Quiet"
    learn = ti.build(TH, 30, [4.0] * 10, [], [], [], 50, [1], 0)
    assert learn.level == 0 and "learning" in learn.components[0].why
    hi = ti.build(TH, 12, [4.0] * 100, ["HMD-1"], ["HMD-2"], [], 9, [2, 2, 3], 0)
    assert hi.level == 3 and hi.word == "High"
    above = [c.key for c in hi.components if c.above]
    assert above == ["military", "mil_alerts", "gnss", "conflict"]
    assert "12 now vs usual 4" in hi.components[0].why and "HMD-1" in hi.components[1].why


def test_endpoint_reads_presence_metrics_and_conflict_events() -> None:
    now = datetime.now(UTC)
    s = MemoryStore()
    hour = now.replace(minute=0, second=0, microsecond=0)
    for h in range(100, 0, -1):
        s.record_metric("Gulf|tanker", mil.METRIC, hour - timedelta(hours=h), 2.0)
    s.record_metric("Gulf|tanker", mil.METRIC, hour, 9.0)
    evs = []
    for d in range(0, 6):
        n = 8 if d == 0 else 1
        for i in range(n):
            evs.append(
                GeoEvent(
                    id=f"g{d}-{i}",
                    ts=now - timedelta(days=d, hours=1),
                    source="gdelt",
                    domain=Domain.EVENTS,
                    kind="conflict_event",
                    lat=26.5,
                    lon=56.2,
                    attrs={"sources": 3},
                )
            )
    s.insert_events(evs)
    c = TestClient(create_app(s, allowed_hosts=["testserver"]))
    d = c.get("/api/military/theatres/index").json()
    gulf = next(t for t in d["theatres"] if t["name"] == "Gulf")
    comps = {x["key"]: x for x in gulf["components"]}
    assert comps["military"]["above"] and comps["military"]["normal"] == 2.0
    assert comps["conflict"]["now"] == 8 and comps["conflict"]["above"]
    assert gulf["level"] == 2 and d["theatres"][0]["name"] == "Gulf"  # highest level first
    assert "Not a threat level" in d["note"]
