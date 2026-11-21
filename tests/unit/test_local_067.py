"""067: local-only build: readiness probe, security headers, loopback-only model (offline)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.brief.local_llm import LocalLLM

HOSTS = ["testserver"]


def test_ready_and_headers() -> None:
    c = TestClient(create_app(MemoryStore(), playbook={}, allowed_hosts=HOSTS))
    r = c.get("/api/ready")
    assert r.status_code == 200 and r.json() == {"ready": True}
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["content-security-policy"].startswith("default-src 'self'")


def test_ready_503_when_store_fails() -> None:
    class Broken(MemoryStore):
        def list_alerts(self, *a: object, **k: object) -> list:  # type: ignore[override]
            raise RuntimeError("db gone")

    c = TestClient(create_app(Broken(), playbook={}, allowed_hosts=HOSTS))
    assert c.get("/api/ready").status_code == 503


def test_model_is_loopback_only() -> None:
    assert LocalLLM({"url": "http://127.0.0.1:11434"}).enabled
    for url in ("http://ollama:11434", "https://api.example.com"):
        assert not LocalLLM({"url": url}).enabled
