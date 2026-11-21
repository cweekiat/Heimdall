"""063: env overlays, readiness probe, forward_auth redirect, same-VM model host (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heimdall import config
from heimdall.adapters.access_file import FileAccessStore
from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.access import AccessGate
from heimdall.api.app import create_app
from heimdall.brief.local_llm import LocalLLM

HOST = "heimdall.example.org"


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("HEIMDALL_ENV", "HEIMDALL_AUTH", "HEIMDALL_ALLOWED_HOSTS", "HEIMDALL_COOKIE_SECURE"):
        monkeypatch.delenv(k, raising=False)
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


def test_deep_merge_keeps_siblings() -> None:
    base = {"storage": {"a": 1, "b": 2}, "x": [1]}
    assert config._deep_merge(base, {"storage": {"b": 3}, "x": [2]}) == {
        "storage": {"a": 1, "b": 3},
        "x": [2],
    }


@pytest.mark.parametrize("env", ["prod", "dev"])
def test_overlays_load(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    base_detectors = config.settings().detectors
    config.settings.cache_clear()
    monkeypatch.setenv("HEIMDALL_ENV", env)
    s = config.settings()
    assert s.api.auth == "access_code" and s.api.host == "0.0.0.0" and s.api.cookie_secure
    assert s.detectors == base_detectors  # overlays never change thresholds


def test_overlays_contain_no_detector_thresholds() -> None:
    import yaml

    for f in (config.CONFIG_DIR / "env").glob("*.yaml"):
        assert "detectors" not in (yaml.safe_load(f.read_text()) or {}), f.name


def test_bad_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEIMDALL_ENV", "../etc")
    with pytest.raises(ValueError):
        config.settings()
    config.settings.cache_clear()
    monkeypatch.setenv("HEIMDALL_ENV", "nope")
    with pytest.raises(FileNotFoundError):
        config.settings()


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEIMDALL_ENV", "prod")
    monkeypatch.setenv("HEIMDALL_AUTH", "none")
    monkeypatch.setenv("HEIMDALL_COOKIE_SECURE", "false")
    monkeypatch.setenv("HEIMDALL_ALLOWED_HOSTS", "a.example, b.example")
    s = config.settings()
    assert s.api.auth == "none" and not s.api.cookie_secure
    assert s.api.allowed_hosts == ["a.example", "b.example"]
    config.settings.cache_clear()
    monkeypatch.setenv("HEIMDALL_AUTH", "open-sesame")
    with pytest.raises(ValueError):
        config.settings()


@pytest.fixture
def hosted(tmp_path: Path) -> TestClient:
    store = FileAccessStore(tmp_path / "access.json")
    store.issue("ana")
    app = create_app(
        MemoryStore(), playbook={}, allowed_hosts=[HOST], access=AccessGate(store, b"k" * 32)
    )
    return TestClient(app, base_url=f"https://{HOST}")


def test_ready_is_public_and_minimal(hosted: TestClient) -> None:
    r = hosted.get("/api/ready")
    assert r.status_code == 200 and r.json() == {"ready": True}


def test_ready_503_when_store_fails(tmp_path: Path) -> None:
    class Broken(MemoryStore):
        def list_alerts(self, *a: object, **k: object) -> list:  # type: ignore[override]
            raise RuntimeError("db gone")

    c = TestClient(create_app(Broken(), playbook={}, allowed_hosts=["testserver"]))
    assert c.get("/api/ready").status_code == 503


def test_forward_auth_redirects_pages_not_api(hosted: TestClient) -> None:
    page = hosted.get(
        "/api/auth/check",
        headers={"X-Forwarded-Uri": "/basemap/world.pmtiles"},
        follow_redirects=False,
    )
    assert page.status_code == 303 and page.headers["location"] == "/login.html"
    api = hosted.get(
        "/api/auth/check", headers={"X-Forwarded-Uri": "/api/alerts"}, follow_redirects=False
    )
    assert api.status_code == 401


def test_model_host_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEIMDALL_ENV", raising=False)
    assert not LocalLLM({"url": "http://ollama:11434"}).enabled  # laptop: loopback only
    monkeypatch.setenv("HEIMDALL_ENV", "prod")
    assert LocalLLM({"url": "http://ollama:11434"}).enabled
    assert not LocalLLM({"url": "http://ollama.evil.example:11434"}).enabled
    assert not LocalLLM({"url": "https://api.example.com"}).enabled


def test_caddy_csp_matches_app() -> None:
    from heimdall.api.app import csp_for

    caddy = (config.ROOT / "deploy" / "Caddyfile").read_text()
    assert f'?Content-Security-Policy "{csp_for("openfreemap")}"' in caddy
