"""014a: write-only keys API (offline, TestClient)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heimdall.adapters.memory_store import MemoryStore
from heimdall.adapters.secrets_file import FileSecretStore
from heimdall.api.app import create_app

HOSTS = ["testserver", "127.0.0.1", "localhost"]
CANARY = "sk-live-SUPERSECRET1234"
LOCAL = {"Origin": "http://testserver"}


class FakeSources:
    def __init__(self) -> None:
        self.restarted: list[str] = []

    def list(self) -> list[Any]:
        return []

    async def set_enabled(self, key: str, enabled: bool) -> Any:
        raise KeyError(key)

    async def restart(self, key: str) -> None:
        self.restarted.append(key)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeSources, Path]:
    for n in ("AISSTREAM_KEY", "FAA_NMS_KEY", "FAA_NMS_KEY", "FAA_NMS_SECRET"):
        monkeypatch.delenv(n, raising=False)
    store = FileSecretStore(tmp_path / "secrets" / "keys.json")
    src = FakeSources()
    app = create_app(MemoryStore(), playbook={}, allowed_hosts=HOSTS, sources=src, secrets=store)
    return TestClient(app, client=("127.0.0.1", 5000)), src, tmp_path


def test_put_list_delete_never_leaks(
    env: tuple[TestClient, FakeSources, Path], caplog: pytest.LogCaptureFixture
) -> None:
    c, src, _ = env
    caplog.set_level(logging.DEBUG)
    r = c.put("/api/keys/FAA_NMS_KEY", json={"value": CANARY}, headers=LOCAL)
    assert r.status_code == 200 and CANARY not in r.text
    assert src.restarted == ["faa_nms_notam"]
    listing = c.get("/api/keys")
    assert CANARY not in listing.text
    row = {k["name"]: k for k in listing.json()}["FAA_NMS_KEY"]
    assert row["is_set"] and row["last4"] == CANARY[-4:]
    r = c.delete("/api/keys/FAA_NMS_KEY", headers=LOCAL)
    assert r.status_code == 200
    assert not {k["name"]: k for k in c.get("/api/keys").json()}["FAA_NMS_KEY"]["is_set"]
    assert src.restarted == ["faa_nms_notam", "faa_nms_notam"]
    assert CANARY not in caplog.text


def test_unknown_name_404(env: tuple[TestClient, FakeSources, Path]) -> None:
    c, src, _ = env
    assert c.put("/api/keys/NOPE", json={"value": CANARY}, headers=LOCAL).status_code == 404
    assert src.restarted == []


def test_origin_and_client_403(env: tuple[TestClient, FakeSources, Path]) -> None:
    c, src, _ = env
    body = {"value": CANARY}
    assert c.put("/api/keys/FAA_NMS_KEY", json=body).status_code == 403
    bad = {"Origin": "http://evil.example"}
    assert c.put("/api/keys/FAA_NMS_KEY", json=body, headers=bad).status_code == 403
    # same-origin fetch metadata alone is not enough for keys
    fetch = {"Sec-Fetch-Site": "same-origin"}
    assert c.put("/api/keys/FAA_NMS_KEY", json=body, headers=fetch).status_code == 403
    remote = TestClient(c.app, client=("203.0.113.9", 5000))
    r = remote.put("/api/keys/FAA_NMS_KEY", json=body, headers=LOCAL)
    assert r.status_code == 403 and CANARY not in r.text
    assert remote.delete("/api/keys/FAA_NMS_KEY", headers=LOCAL).status_code == 403
    assert src.restarted == []


@pytest.mark.parametrize(
    "payload", [{"value": 5}, {"other": CANARY}, {"value": CANARY + " x y"}, [CANARY]]
)
def test_malformed_body_no_echo(env: tuple[TestClient, FakeSources, Path], payload: Any) -> None:
    c, src, _ = env
    r = c.put("/api/keys/FAA_NMS_KEY", json=payload, headers=LOCAL)
    assert r.status_code == 422 and CANARY not in r.text
    r = c.put("/api/keys/FAA_NMS_KEY", content=CANARY + "{", headers=LOCAL)
    assert r.status_code == 422 and CANARY not in r.text
    assert src.restarted == []


def test_jwt_length_earthdata_token_saves(env: tuple[TestClient, FakeSources, Path]) -> None:
    """Earthdata tokens are ~1 kB JWTs; the old 512-char cap rejected them (bug 2026-10-01)."""
    c, src, _ = env
    jwt_like = "hdr." + "a" * 900 + "." + "b" * 300
    r = c.put("/api/keys/EARTHDATA_TOKEN", json={"value": jwt_like}, headers=LOCAL)
    assert r.status_code == 200 and jwt_like not in r.text and src.restarted == ["smap_rfi"]
    assert c.put("/api/keys/EARTHDATA_TOKEN", json={"value": "x" * 5000}, headers=LOCAL).json() == {
        "detail": "key too long (max 4096 characters)"
    }
