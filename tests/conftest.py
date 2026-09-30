"""Suite-wide isolation: tests never see the real saved keys in data/secrets (ADR-006)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import heimdall.adapters.secrets_file as secrets_file


@pytest.fixture(autouse=True)
def _isolated_secret_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("HEIMDALL_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(secrets_file, "_default", None)
    yield
    secrets_file._default = None
