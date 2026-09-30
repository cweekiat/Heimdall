"""File-backed SecretStore (ADR-006): `data/secrets/keys.json`, mode 0600, dir 0700.

Store wins over the environment; the environment is the fallback. `.env` is never read.
Values are never logged. No encryption at rest (out of scope).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

from heimdall.logging import get_logger
from heimdall.schemas import KEY_CONNECTORS, KeyStatus

log = get_logger("heimdall.secrets")


class FileSecretStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, str]:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, str) and v}

    def _write(self, data: dict[str, str]) -> None:
        d = self._path.parent
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".keys-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def get(self, name: str) -> str | None:
        with self._lock:
            v = self._read().get(name)
        return v or os.environ.get(name) or None

    def set(self, name: str, value: str) -> None:
        with self._lock:
            data = self._read()
            data[name] = value
            self._write(data)
        log.info("secret_set", name=name)

    def delete(self, name: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(name, None) is not None:
                self._write(data)
        log.info("secret_deleted", name=name)

    def status(self) -> list[KeyStatus]:
        with self._lock:
            stored = self._read()
        out: list[KeyStatus] = []
        for name, conn in KEY_CONNECTORS.items():
            if name in stored:
                val, origin = stored[name], "store"
            elif os.environ.get(name):
                val, origin = os.environ[name], "env"
            else:
                out.append(KeyStatus(name=name, connector=conn))
                continue
            out.append(
                KeyStatus(
                    name=name,
                    connector=conn,
                    is_set=True,
                    last4=val[-4:] if len(val) >= 8 else None,
                    origin=origin,  # type: ignore[arg-type]
                )
            )
        return out


_default: FileSecretStore | None = None


def default_store() -> FileSecretStore:
    """Process-wide store at `<repo>/data/secrets/keys.json` (override: HEIMDALL_SECRETS_DIR)."""
    global _default
    if _default is None:
        from heimdall.config import ROOT

        base = os.environ.get("HEIMDALL_SECRETS_DIR")
        _default = FileSecretStore(
            (Path(base) if base else ROOT / "data" / "secrets") / "keys.json"
        )
    return _default
