"""Hosted-mode access gate (task 062, ADR-009): access-code login and signed session cookies.

Sessions are stateless (HMAC-signed name + gen + expiry), so any API replica with the same key
verifies them. Re-issuing or revoking a code bumps `gen`, which ends that person's sessions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from heimdall import config
from heimdall.adapters.access_file import AccessUser, FileAccessStore, load_or_create_key
from heimdall.config import Settings
from heimdall.logging import get_logger

log = get_logger("heimdall.api.access")

COOKIE_SECURE = "__Host-hm_session"  # __Host-: Secure, Path=/, no Domain (browser enforced)
COOKIE_PLAIN = "hm_session"  # local http testing only (api.cookie_secure: false)
# Reachable without a session: the login page itself and the login/session probes.
PUBLIC_PATHS = frozenset(
    {
        "/login.html",
        "/login.js",
        "/login.css",
        "/favicon.svg",
        "/api/login",
        "/api/session",
        "/api/ready",
    }
)


class LoginIn(BaseModel):
    code: str = Field(min_length=1, max_length=200)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass
class RateLimiter:
    """Failed-login limiter: per client and global sliding windows (in memory, per process)."""

    per_client: int = 5
    per_client_window_s: float = 60.0
    global_max: int = 30
    global_window_s: float = 600.0
    _clients: dict[str, deque[float]] = field(default_factory=dict)
    _global: deque[float] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def blocked(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._trim(now)
            q = self._clients.get(client)
            return (q is not None and len(q) >= self.per_client) or len(
                self._global
            ) >= self.global_max

    def fail(self, client: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._clients.setdefault(client, deque()).append(now)
            self._global.append(now)
            self._trim(now)

    def _trim(self, now: float) -> None:
        while self._global and now - self._global[0] > self.global_window_s:
            self._global.popleft()
        for k in list(self._clients):
            q = self._clients[k]
            while q and now - q[0] > self.per_client_window_s:
                q.popleft()
            if not q:
                del self._clients[k]


class AccessGate:
    def __init__(
        self,
        store: FileAccessStore,
        key: bytes,
        session_days: float = 7,
        secure_cookie: bool = True,
        trusted_proxies: list[str] | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.store = store
        self._key = key
        self.ttl_s = int(session_days * 86400)
        self.secure = secure_cookie
        self.cookie = COOKIE_SECURE if secure_cookie else COOKIE_PLAIN
        self._proxies = [
            ipaddress.ip_network(p, strict=False) for p in (trusted_proxies or ["127.0.0.1", "::1"])
        ]
        self.limiter = limiter or RateLimiter()

    # ------------------------------------------------------------------ sessions
    def _sig(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())

    def issue(self, user: AccessUser, now: float | None = None) -> str:
        exp = int((time.time() if now is None else now) + self.ttl_s)
        payload = _b64(json.dumps({"u": user.name, "g": user.gen, "e": exp}).encode())
        return f"{payload}.{self._sig(payload)}"

    def verify(self, token: str | None, now: float | None = None) -> AccessUser | None:
        if not token or len(token) > 1000 or "." not in token:
            return None
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(payload)):
            return None
        try:
            data = json.loads(_unb64(payload))
            name, gen, exp = str(data["u"]), int(data["g"]), int(data["e"])
        except (ValueError, KeyError, TypeError):
            return None
        if exp < (time.time() if now is None else now):
            return None
        live = self.store.current(name)  # revoked or re-issued -> session ends
        if live is None or live.gen != gen:
            return None
        return live

    def user_for(self, request: Request) -> AccessUser | None:
        return self.verify(request.cookies.get(self.cookie))

    # ------------------------------------------------------------------ client ip
    def client_ip(self, request: Request) -> str:
        """Peer address; proxy headers are honoured only when the peer is a trusted proxy."""
        peer = request.client.host if request.client else ""
        try:
            trusted = any(ipaddress.ip_address(peer) in n for n in self._proxies)
        except ValueError:
            trusted = False
        if trusted:
            fwd = (
                request.headers.get("cf-connecting-ip")
                or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            )
            if fwd:
                return fwd
        return peer or "unknown"

    def set_cookie(self, resp: Response, token: str) -> None:
        resp.set_cookie(
            self.cookie,
            token,
            max_age=self.ttl_s,
            path="/",
            secure=self.secure,
            httponly=True,
            samesite="strict",
        )

    def clear_cookie(self, resp: Response) -> None:
        resp.delete_cookie(
            self.cookie, path="/", secure=self.secure, httponly=True, samesite="strict"
        )


def current_user(request: Request) -> str:
    """Name for audit fields (notes, feedback). 'local' when hosted mode is off."""
    return str(getattr(request.state, "user", None) or "local")


def access_router(gate: AccessGate | None) -> APIRouter:
    r = APIRouter()

    @r.get("/api/session")
    def session(request: Request) -> dict[str, Any]:
        if gate is None:
            return {"auth": False, "name": "local", "admin": True}
        u = gate.user_for(request)
        return {
            "auth": True,
            "signed_in": u is not None,
            "name": u.name if u else None,
            "admin": bool(u and u.admin),
        }

    if gate is None:
        return r

    @r.post("/api/login")
    async def login(request: Request) -> Response:
        ip = gate.client_ip(request)
        if gate.limiter.blocked(ip):
            log.warning("login_rate_limited", client=ip)
            return JSONResponse({"detail": "too many attempts, wait a minute"}, status_code=429)
        try:
            body = LoginIn.model_validate(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse({"detail": "enter your access code"}, status_code=422)
        user = gate.store.verify(body.code)  # never logged
        if user is None:
            gate.limiter.fail(ip)
            log.warning("login_failed", client=ip)
            return JSONResponse({"detail": "access code not recognised"}, status_code=401)
        resp = JSONResponse({"name": user.name, "admin": user.admin})
        gate.set_cookie(resp, gate.issue(user))
        log.info("login_ok", user=user.name, client=ip)
        return resp

    @r.post("/api/logout")
    def logout() -> Response:
        resp = JSONResponse({"signed_out": True})
        gate.clear_cookie(resp)
        return resp

    @r.get("/api/auth/check")
    def auth_check() -> Response:
        # Reached only through the gate, so a valid session already passed: for proxy
        # forward_auth (Caddy serving static files and tiles, task 063).
        return Response(status_code=204)

    return r


def secrets_dir() -> Path:
    base = os.environ.get("HEIMDALL_SECRETS_DIR")
    return Path(base) if base else config.ROOT / "data" / "secrets"


def gate_from_settings(cfg: Settings) -> AccessGate | None:
    """Hosted mode on -> a gate over data/secrets/access.json; off -> None (localhost tool)."""
    if cfg.api.auth != "access_code":
        return None
    d = secrets_dir()
    store = FileAccessStore(d / "access.json")
    if not store.has_users():
        log.warning(
            "access_no_codes",
            note="hosted mode is on but no access codes exist: nobody can sign in. "
            "Issue one: uv run python scripts/access_codes.py add NAME --admin",
        )
    return AccessGate(
        store,
        load_or_create_key(d / "session.key"),
        session_days=cfg.api.session_days,
        secure_cookie=cfg.api.cookie_secure,
        trusted_proxies=cfg.api.trusted_proxies,
    )
