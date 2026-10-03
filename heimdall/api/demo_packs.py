"""Demo packs on the live server (task 043).

A demo pack is a precomputed JSON file (`data/replay/*/*.demo.json`, written by e.g.
`scripts/midnight_hammer.py`) holding a scenario's steps, a snapshot of the aircraft at each step,
and the alerts + evidence Heimdall raised from that real recording. The live server only reads and
serves these files: no replay store, no pipeline, no effect on live detection. A pack is loaded on
first use and cached by file modification time (a few MB at most).

GET /api/demo/packs          -> [{name, title, summary, n_steps}]
GET /api/demo/packs/{name}   -> the pack (steps with snapshots, alert list)
Alert details of demo alerts are served by `/api/alerts/{id}` through `lookup_alert` (marked
`demo: true`, read-only: the UI offers no triage actions on them).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from heimdall.logging import get_logger

log = get_logger("heimdall.demo_packs")

DEFAULT_ROOT = Path("data/replay")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
MAX_BYTES = 50 << 20


class DemoPacks:
    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = root
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def paths(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for p in sorted(self.root.glob("*/*.demo.json")):
            name = p.name.removesuffix(".demo.json")
            if NAME_RE.match(name):
                out[name] = p
        return out

    def load(self, name: str) -> dict[str, Any] | None:
        p = self.paths().get(name)
        if p is None:
            return None
        try:
            st = p.stat()
            hit = self._cache.get(name)
            if hit is not None and hit[0] == st.st_mtime:
                return hit[1]
            if st.st_size > MAX_BYTES:
                log.warning("demo_pack_too_large", name=name, bytes=st.st_size)
                return None
            data = json.loads(p.read_text())
        except (OSError, ValueError) as e:
            log.warning("demo_pack_unreadable", name=name, error=type(e).__name__)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            return None
        self._cache[name] = (st.st_mtime, data)
        return data

    def summaries(self) -> list[dict[str, Any]]:
        out = []
        for name in self.paths():
            d = self.load(name)
            if d is not None:
                out.append(
                    {
                        "name": name,
                        "title": d.get("title", name),
                        "summary": d.get("summary", ""),
                        "n_steps": len(d["steps"]),
                    }
                )
        return out

    def lookup_alert(self, alert_id: str) -> dict[str, Any] | None:
        """AlertDetail-shaped dict for a demo alert, or None."""
        for name in self.paths():
            d = self.load(name)
            if d is None:
                continue
            a = next((x for x in d.get("alerts", []) if x.get("id") == alert_id), None)
            if a is None:
                continue
            ids = set(a.get("evidence_ids", []))
            ev = [e for e in d.get("evidence", []) if e.get("id") in ids]
            return {
                "alert": a,
                "evidence": ev,
                "feedback": [],
                "notes": [],
                "actions": [],
                "playbook": [],
                "demo": True,
                "pack": name,
            }
        return None


def demo_router(packs: DemoPacks) -> APIRouter:
    r = APIRouter()

    @r.get("/api/demo/packs")
    def list_packs() -> list[dict[str, Any]]:
        return packs.summaries()

    @r.get("/api/demo/packs/{name}")
    def get_pack(name: str) -> dict[str, Any]:
        if not NAME_RE.match(name):
            raise HTTPException(404, "unknown demo")
        d = packs.load(name)
        if d is None:
            raise HTTPException(404, "unknown demo")
        # evidence stays server-side (served per alert); the page needs steps + the alert list
        return {k: v for k, v in d.items() if k != "evidence"}

    return r
