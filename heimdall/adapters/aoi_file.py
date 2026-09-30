"""File-backed AoiStore: `data/state/aois.json` (atomic replace). Also an in-memory variant."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from heimdall.logging import get_logger
from heimdall.schemas import MAX_USER_AOIS, UserAoi

log = get_logger("heimdall.aoi_store")


class FileAoiStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> list[UserAoi]:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("aoi_state_unreadable", error=type(e).__name__)
            return []
        if not isinstance(raw, list):
            log.warning("aoi_state_malformed")
            return []
        out: list[UserAoi] = []
        for row in raw[:MAX_USER_AOIS]:
            try:
                out.append(UserAoi.model_validate(row))
            except ValidationError:
                log.warning("aoi_state_row_skipped")
        return out

    def save(self, aois: Sequence[UserAoi]) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps([a.model_dump(mode="json") for a in aois], indent=1))
            os.replace(tmp, self._path)


class MemoryAoiStore:
    def __init__(self) -> None:
        self._rows: list[UserAoi] = []

    def load(self) -> list[UserAoi]:
        return list(self._rows)

    def save(self, aois: Sequence[UserAoi]) -> None:
        self._rows = list(aois)
