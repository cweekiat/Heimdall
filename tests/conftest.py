"""Suite-wide isolation: tests never see real saved keys (ADR-006) or military state (039)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import heimdall.adapters.secrets_file as secrets_file


@pytest.fixture(autouse=True)
def _isolated_secret_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("HEIMDALL_SECRETS_DIR", str(tmp_path / "secrets"))
    # task 039: military track state and the plane-alert list never come from data/
    monkeypatch.setenv("HEIMDALL_MIL_STATE", "")
    monkeypatch.setenv("HEIMDALL_PLANE_ALERT", str(tmp_path / "plane-alert-db.csv"))
    monkeypatch.setenv("HEIMDALL_AIRPORTS", str(tmp_path / "airports.csv"))  # 047
    monkeypatch.setattr(secrets_file, "_default", None)
    yield
    secrets_file._default = None
