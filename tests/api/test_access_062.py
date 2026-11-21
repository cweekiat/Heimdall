"""062: hosted mode — access codes, session cookies, gating, admin writes, proxy trust (offline)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from heimdall.adapters.access_file import FileAccessStore, normalize
from heimdall.adapters.memory_store import MemoryStore
from heimdall.adapters.secrets_file import FileSecretStore
from heimdall.api.access import AccessGate, RateLimiter
from heimdall.api.app import create_app

HOST = "heimdall.example.org"
ORIGIN = {"Origin": f"https://{HOST}"}


class FakeSources:
    def list(self) -> list[Any]:
        return []

    async def set_enabled(self, key: str, enabled: bool) -> Any:
        raise KeyError(key)

    async def restart(self, key: str) -> None:
        return None


@pytest.fixture
def setup(tmp_path: Path) -> tuple[TestClient, FileAccessStore, AccessGate, dict[str, str]]:
    store = FileAccessStore(tmp_path / "secrets" / "access.json")
    codes = {
        "boss": store.issue("boss", admin=True),
        "ana": store.issue("ana"),
    }
    gate = AccessGate(store, b"k" * 32, trusted_proxies=["10.0.0.0/8"])
    app = create_app(
        MemoryStore(),
        playbook={},
        allowed_hosts=[HOST],
        sources=FakeSources(),
        secrets=FileSecretStore(tmp_path / "secrets" / "keys.json"),
        access=gate,
    )
    c = TestClient(app, base_url=f"https://{HOST}", client=("203.0.113.9", 5000))
    return c, store, gate, codes


def login(c: TestClient, code: str) -> Any:
    return c.post("/api/login", json={"code": code}, headers=ORIGIN)


def test_codes_hashed_and_normalized(tmp_path: Path) -> None:
    store = FileAccessStore(tmp_path / "a.json")
    code = store.issue("x")
    text = (tmp_path / "a.json").read_text()
    assert normalize(code) not in text and code not in text
    assert oct((tmp_path / "a.json").stat().st_mode & 0o777) == "0o600"
    assert store.verify(code.lower().replace("-", " ")) is not None
    with pytest.raises(ValueError):
        store.issue("y", code="short")
    store.issue("y", code="my-own-long-code")
    assert store.verify("MY OWN LONG CODE") is not None
    with pytest.raises(ValueError):
        store.issue("z", code="my-own-long-code")  # no shared codes


def test_everything_gated_except_login(setup: Any) -> None:
    c, *_ = setup
    assert c.get("/api/alerts").status_code == 401
    assert c.get("/api/health").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login.html"
    s = c.get("/api/session").json()
    assert s == {"auth": True, "signed_in": False, "name": None, "admin": False}


def test_login_cookie_and_logout(setup: Any) -> None:
    c, _, gate, codes = setup
    assert login(c, "WRONG-CODE-XXXX").status_code == 401
    r = login(c, codes["ana"])
    assert r.status_code == 200 and r.json() == {"name": "ana", "admin": False}
    sc = r.headers["set-cookie"]
    assert gate.cookie in sc and "HttpOnly" in sc and "Secure" in sc
    assert "samesite=strict" in sc.lower()
    assert c.get("/api/alerts").status_code == 200
    h = c.get("/api/alerts").headers
    assert "max-age=31536000" in h["strict-transport-security"]
    assert c.post("/api/logout", headers=ORIGIN).status_code == 200
    assert c.get("/api/alerts").status_code == 401


def test_login_needs_same_origin(setup: Any) -> None:
    c, _, _, codes = setup
    r = c.post("/api/login", json={"code": codes["ana"]}, headers={"Origin": "https://evil.test"})
    assert r.status_code == 403


def test_tampered_expired_revoked_reissued(setup: Any) -> None:
    c, store, gate, codes = setup
    user = store.current("ana")
    assert user is not None
    tok = gate.issue(user)
    assert gate.verify(tok) is not None
    payload, sig = tok.rsplit(".", 1)
    assert gate.verify(payload + "." + sig[:-2] + "AA") is None
    assert gate.verify(gate.issue(user, now=time.time() - 8 * 86400)) is None
    other = AccessGate(store, b"z" * 32)
    assert other.verify(tok) is None  # different server key
    store.issue("ana")  # re-issue: old sessions end
    assert gate.verify(tok) is None
    login(c, codes["boss"])
    store.revoke("boss")
    assert c.get("/api/alerts").status_code == 401
    assert login(c, codes["boss"]).status_code == 401


def test_rate_limit(setup: Any, caplog: pytest.LogCaptureFixture) -> None:
    c, _, _, codes = setup
    caplog.set_level(logging.DEBUG)
    for _ in range(5):
        assert login(c, "BAD-CODE-000000").status_code == 401
    assert login(c, codes["ana"]).status_code == 429  # even a right code waits
    assert codes["ana"] not in caplog.text and "BAD-CODE-000000" not in caplog.text


def test_rate_limiter_windows() -> None:
    rl = RateLimiter(per_client=2, per_client_window_s=60, global_max=3, global_window_s=600)
    rl.fail("a", now=0)
    rl.fail("a", now=1)
    assert rl.blocked("a", now=2) and not rl.blocked("b", now=2)
    assert not rl.blocked("a", now=70)
    rl.fail("b", now=71)
    assert rl.blocked("c", now=72)  # global
    assert not rl.blocked("c", now=700)


def test_proxy_headers_only_from_trusted_peer(setup: Any) -> None:
    _, store, _, _ = setup
    gate = AccessGate(store, b"k" * 32, trusted_proxies=["10.0.0.0/8"])

    class Req:
        def __init__(self, peer: str, headers: dict[str, str]) -> None:
            self.client = type("C", (), {"host": peer})()
            self.headers = headers

    spoof = {"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"}
    assert gate.client_ip(Req("203.0.113.9", spoof)) == "203.0.113.9"  # type: ignore[arg-type]
    assert gate.client_ip(Req("10.1.2.3", spoof)) == "1.2.3.4"  # type: ignore[arg-type]
    xff = {"x-forwarded-for": "5.6.7.8, 10.0.0.1"}
    assert gate.client_ip(Req("10.1.2.3", xff)) == "10.1.2.3"  # type: ignore[arg-type]


def test_admin_only_writes_and_attribution(setup: Any) -> None:
    c, _, _, codes = setup
    login(c, codes["ana"])
    assert (
        c.put("/api/keys/FAA_NMS_KEY", json={"value": "x" * 20}, headers=ORIGIN).status_code == 403
    )
    assert (
        c.post("/api/sources/adsb_lol", json={"enabled": False}, headers=ORIGIN).status_code == 403
    )
    assert c.post("/api/alerts/clear", headers=ORIGIN).status_code == 403
    login(c, codes["boss"])
    assert (
        c.put("/api/keys/FAA_NMS_KEY", json={"value": "x" * 20}, headers=ORIGIN).status_code == 200
    )
    assert c.post("/api/alerts/clear", headers=ORIGIN).status_code == 200


def test_feedback_records_user(tmp_path: Path) -> None:
    from heimdall.api.demo import seed

    store = FileAccessStore(tmp_path / "access.json")
    code = store.issue("ana")
    app = create_app(
        seed(MemoryStore()), playbook={}, allowed_hosts=[HOST], access=AccessGate(store, b"k" * 32)
    )
    c = TestClient(app, base_url=f"https://{HOST}")
    login(c, code)
    body = {"verdict": "dismiss", "reason": "dup"}
    r = c.post("/api/alerts/alert-0/feedback", json=body, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["user"] == "ana"
    notes = c.get("/api/alerts/alert-0/notes").json()
    assert notes and notes[-1]["user"] == "ana"


def test_foreign_host_rejected(setup: Any) -> None:
    c, _, _, codes = setup
    login(c, codes["ana"])
    r = c.get("/api/alerts", headers={"host": "attacker.test"})
    assert r.status_code in (400, 401)


def test_local_mode_unchanged(tmp_path: Path) -> None:
    app = create_app(MemoryStore(), playbook={}, allowed_hosts=["testserver"])
    c = TestClient(app)
    assert c.get("/api/alerts").status_code == 200
    assert c.get("/api/session").json()["auth"] is False
    assert "strict-transport-security" not in c.get("/api/alerts").headers
