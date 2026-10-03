"""Shift brief API (task 044): rule-based trends always; local-model narration when available.

GET  /api/brief/status          -> {available, model, reason}       (no facts sent)
GET  /api/brief/trends?hours=8  -> {facts, trends}                  (no model)
POST /api/brief/summary {hours} -> {facts, trends, narrative, model, cited, unsupported}
The model only ever sees the fact sheet (heimdall.brief.facts), and only on this machine.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from heimdall import config
from heimdall.api.spectrum import band_status
from heimdall.brief.facts import build_facts, trend_lines
from heimdall.brief.local_llm import LocalLLM
from heimdall.ports import Store


class SummaryIn(BaseModel):
    hours: float = Field(8.0, ge=1, le=72)


def _facts(store: Store, hours: float) -> dict[str, Any]:
    now = datetime.now(UTC)
    alerts = store.list_alerts(limit=5000)
    try:
        board = band_status(store, now)
        states = [
            {"area": a.get("label") or a.get("aoi"), "band": b, "state": cell.get("state")}
            for a in board.get("areas", [])
            for b, cell in (a.get("bands") or {}).items()
        ]
    except Exception:  # the board is context only; never block the brief on it
        states = []
    labels = {a.name: (a.label or a.name) for a in config.settings().aois}
    return build_facts(alerts, now, hours, states, area_labels=labels)


def brief_router(store: Store) -> APIRouter:
    r = APIRouter()
    llm = LocalLLM(config.settings().brief.llm.model_dump())
    cache: dict[str, tuple[float, Any]] = {}

    @r.get("/api/brief/status")
    def status() -> dict[str, Any]:
        hit = cache.get("status")
        if hit and time.monotonic() - hit[0] < 30:
            return hit[1]
        out = llm.status()
        cache["status"] = (time.monotonic(), out)
        return out

    @r.get("/api/brief/trends")
    def trends(hours: float = Query(8.0, ge=1, le=72)) -> dict[str, Any]:
        f = _facts(store, hours)
        return {"facts": f, "trends": trend_lines(f)}

    @r.post("/api/brief/summary")
    def summary(body: SummaryIn) -> dict[str, Any]:
        f = _facts(store, body.hours)
        out: dict[str, Any] = {"facts": f, "trends": trend_lines(f)}
        try:
            res = llm.summarise(f)
        except (RuntimeError, httpx.HTTPError, ValueError) as e:
            raise HTTPException(503, f"local model unavailable: {type(e).__name__}") from e
        out.update(
            narrative=res.text, model=res.model, cited=res.cited, unsupported=res.unsupported
        )
        return out

    return r
