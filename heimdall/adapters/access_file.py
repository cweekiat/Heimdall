"""File-backed access codes for hosted mode (task 062, ADR-009).

`data/secrets/access.json`, mode 0600, dir 0700 (same atomic-write pattern as FileSecretStore).
Codes are stored only as salted scrypt hashes; a code is shown once when issued and never logged.
Each person has a `gen` counter: re-issuing or revoking bumps it, which invalidates that person's
sessions. The file is re-read when it changes on disk, so the CLI can manage a running server.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from heimdall.logging import get_logger

log = get_logger("heimdall.access")

# No 0/O/1/I/L: codes are read aloud and typed from chat messages.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
GENERATED_LEN = 20  # ~99 bits
MIN_CUSTOM_LEN = 12
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}  # ~16 MB, tens of ms per check


@dataclass(frozen=True)
class AccessUser:
    name: str
    admin: bool
    gen: int


def normalize(code: str) -> str:
    """Generated codes are shown grouped (ABCDE-FGHJK-...); typing is case/space/dash tolerant."""
    return re.sub(r"[\s-]", "", code).upper()


def generate_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(GENERATED_LEN))
    return "-".join(raw[i : i + 5] for i in range(0, GENERATED_LEN, 5))


def _hash(code: str, salt: bytes) -> str:
    return hashlib.scrypt(normalize(code).encode(), salt=salt, **_SCRYPT).hex()


class FileAccessStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._mtime: int | None = None

    # ------------------------------------------------------------------ file io
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            mtime = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            self._cache, self._mtime = {}, None
            return self._cache
        if mtime != self._mtime:
            try:
                raw = json.loads(self._path.read_text())
                users = raw.get("users", {}) if isinstance(raw, dict) else {}
                self._cache = {k: v for k, v in users.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                # Fail closed: an unreadable file means nobody can sign in, never "open".
                log.error("access_file_unreadable")
                self._cache = {}
            self._mtime = mtime
        return self._cache

    def _write(self, users: dict[str, dict[str, Any]]) -> None:
        d = self._path.parent
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".access-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"version": 1, "users": users}, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._mtime = None  # force reload

    # ------------------------------------------------------------------ admin (CLI)
    def issue(self, name: str, admin: bool = False, code: str | None = None) -> str:
        """Create or re-issue `name`'s code. Returns the code (shown once). Re-issue = new gen."""
        if not NAME_RE.match(name):
            raise ValueError("name: 1-40 chars, letters, digits, _ . -")
        if code is None:
            code = generate_code()
        elif len(normalize(code)) < MIN_CUSTOM_LEN:
            raise ValueError(f"custom code must have at least {MIN_CUSTOM_LEN} characters")
        salt = secrets.token_bytes(16)
        with self._lock:
            users = dict(self._load())
            for other, u in users.items():  # two people must never share a code
                if other != name and not u.get("revoked") and self._match(u, code):
                    raise ValueError("that code is already in use")
            prev = users.get(name, {})
            users[name] = {
                "salt": salt.hex(),
                "hash": _hash(code, salt),
                "admin": admin,
                "gen": int(prev.get("gen", 0)) + 1,
                "created": datetime.now(UTC).isoformat(timespec="seconds"),
                "revoked": False,
            }
            self._write(users)
        log.info("access_code_issued", user=name, admin=admin)
        return code

    def revoke(self, name: str) -> bool:
        with self._lock:
            users = dict(self._load())
            u = users.get(name)
            if u is None:
                return False
            users[name] = {**u, "revoked": True, "gen": int(u.get("gen", 0)) + 1}
            self._write(users)
        log.info("access_code_revoked", user=name)
        return True

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            users = self._load()
        return [
            {
                "name": k,
                "admin": bool(v.get("admin")),
                "revoked": bool(v.get("revoked")),
                "created": v.get("created"),
            }
            for k, v in sorted(users.items())
        ]

    # ------------------------------------------------------------------ runtime
    @staticmethod
    def _match(u: dict[str, Any], code: str) -> bool:
        try:
            want = str(u["hash"])
            got = _hash(code, bytes.fromhex(str(u["salt"])))
        except (KeyError, ValueError):
            return False
        return hmac.compare_digest(want, got)

    def verify(self, code: str) -> AccessUser | None:
        """Check every active entry (no early exit), so timing does not reveal which one matched."""
        if not code or len(code) > 200:
            return None
        with self._lock:
            users = dict(self._load())
        found: AccessUser | None = None
        for name, u in users.items():
            if u.get("revoked"):
                continue
            if self._match(u, code) and found is None:
                found = AccessUser(name, bool(u.get("admin")), int(u.get("gen", 0)))
        return found

    def current(self, name: str) -> AccessUser | None:
        """Live state for a session check (cheap: cached until the file changes)."""
        with self._lock:
            u = self._load().get(name)
        if u is None or u.get("revoked"):
            return None
        return AccessUser(name, bool(u.get("admin")), int(u.get("gen", 0)))

    def has_users(self) -> bool:
        with self._lock:
            return any(not u.get("revoked") for u in self._load().values())


def load_or_create_key(path: Path) -> bytes:
    """Session-signing key (32 random bytes, 0600). Deleting it signs everyone out."""
    try:
        key = path.read_bytes()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key
