"""053: daily SITREP - deterministic Markdown from Heimdall's own facts."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.brief.facts import build_facts
from heimdall.brief.sitrep import build_sitrep

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def test_sitrep_sections_theatres_and_space_weather() -> None:
    facts = build_facts([], NOW, 24)
    th = [
        {
            "name": "Gulf",
            "level": 2,
            "word": "Elevated",
            "components": [
                {"above": True, "why": "12 now vs usual 4"},
                {"above": True, "why": "1 open: HMD-1"},
                {"above": False, "why": "none open"},
            ],
        },
        {"name": "Baltic", "level": 0, "word": "Quiet", "components": []},
    ]
    md = build_sitrep(
        facts,
        th,
        {
            "contacts": 5,
            "window_min": 60,
            "roles": {"tanker": 2},
            "high_interest": [{"flight": "SAM|1", "type_name": "VC-25", "operator": "USAF"}],
        },
        {"kp_max": 6.0, "flare": "X1.2 at 08:10 UTC"},
        NOW,
    )
    assert md.startswith("# Heimdall daily SITREP — 03 Oct 2026 (UTC)")
    assert "**Gulf: Elevated** — 12 now vs usual 4; 1 open: HMD-1" in md and "Baltic" not in md
    assert "No alerts in the period." in md and "SAM/1" in md  # table-safe text
    assert "storm" in md and "X1.2" in md


def test_sitrep_endpoint() -> None:
    c = TestClient(create_app(MemoryStore(), allowed_hosts=["testserver"]))
    d = c.get("/api/brief/sitrep").json()
    assert d["markdown"].startswith("# Heimdall daily SITREP") and "## 6. Limits" in d["markdown"]
