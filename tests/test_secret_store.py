"""FileSecretStore: modes, precedence, env fallback, no leakage in logs (offline)."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest

from heimdall.adapters.secrets_file import FileSecretStore
from heimdall.ports import SecretStore

CANARY = "sk-live-SUPERSECRET1234"


def test_set_get_modes_and_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store = FileSecretStore(tmp_path / "secrets" / "keys.json")
    assert isinstance(store, SecretStore)
    monkeypatch.setenv("FAA_NMS_KEY", "from-env-value")
    assert store.get("FAA_NMS_KEY") == "from-env-value"
    store.set("FAA_NMS_KEY", CANARY)
    f = tmp_path / "secrets" / "keys.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert store.get("FAA_NMS_KEY") == CANARY
    st = {k.name: k for k in store.status()}["FAA_NMS_KEY"]
    assert st.is_set and st.origin == "store" and st.last4 == CANARY[-4:]
    assert CANARY not in st.model_dump_json()
    store.delete("FAA_NMS_KEY")
    assert store.get("FAA_NMS_KEY") == "from-env-value"
    assert not list(f.parent.glob("*.tmp"))
    assert CANARY not in caplog.text


def test_unset_and_corrupt_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AISSTREAM_KEY", raising=False)
    p = tmp_path / "keys.json"
    p.write_text("{not json")
    store = FileSecretStore(p)
    assert store.get("AISSTREAM_KEY") is None
    assert {k.name: k for k in store.status()}["AISSTREAM_KEY"].is_set is False


def test_corrupt_file_is_not_overwritten(tmp_path: Path) -> None:
    p = tmp_path / "keys.json"
    p.write_text("{not json")
    store = FileSecretStore(p)
    with pytest.raises(RuntimeError):
        store.set("AISSTREAM_KEY", "abcdefghijklmnop")
    assert p.read_text() == "{not json"
