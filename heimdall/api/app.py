"""FastAPI app for the analyst UI. Read-mostly; actions are limited to config/playbook.yaml ids."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from heimdall import config
from heimdall.ports import Store
from heimdall.schemas import (
    ActionRequest,
    Alert,
    AlertStatus,
    Feedback,
)

WEB_DIST = config.ROOT / "web" / "dist"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]


class FeedbackIn(BaseModel):
    verdict: Literal["confirm", "dismiss"]
    reason: str | None = None


class ActionIn(BaseModel):
    action_id: str


def _now() -> datetime:
    return datetime.now(UTC)


def _action_json(a: ActionRequest) -> dict[str, Any]:
    return a.model_dump(mode="json")


def _since(value: str | None, default_hours: float | None = None) -> datetime | None:
    if value:
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(422, f"invalid since: {value}") from e
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    if default_hours is not None:
        return _now() - timedelta(hours=default_hours)
    return None


def create_app(
    store: Store,
    playbook: dict[str, Any] | None = None,
    auth_check: Callable[[Request], Awaitable[bool]] | None = None,
    stream_interval_s: float = 2.0,
    allowed_hosts: list[str] | None = None,
    basemap: str | None = None,
) -> FastAPI:
    st: Store = store
    pb: dict[str, Any] = (playbook if playbook is not None else config.playbook()).get(
        "actions", {}
    )
    app = FastAPI(title="Heimdall API", version="0.1")
    app.state.store = st
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOCAL_HOSTS)

    @app.middleware("http")
    async def same_origin_writes(
        request: Request, call_next: Callable[..., Awaitable[Response]]
    ) -> Response:
        # CSRF/DNS-rebinding guard: a browser POST must come from the page's own origin.
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
                return Response("cross-origin request rejected", status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def auth_middleware(
        request: Request, call_next: Callable[..., Awaitable[Response]]
    ) -> Response:
        # Hook: no-op when api.auth == none. Hosted deployments plug in auth_check (e.g. OIDC).
        if (
            auth_check is not None
            and config.settings().api.auth != "none"
            and request.url.path.startswith("/api")
            and not await auth_check(request)
        ):
            return Response("unauthorized", status_code=401)
        return await call_next(request)

    def playbook_for(kind: str) -> list[dict[str, Any]]:
        return [
            {"id": k, **v}
            for k, v in pb.items()
            if "*" in v.get("applies_to", []) or kind in v.get("applies_to", [])
        ]

    def need_alert(alert_id: str) -> Alert:
        a = st.get_alert(alert_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        return a

    # ------------------------------------------------------------------ alerts
    @app.get("/api/alerts")
    def list_alerts(
        status: str | None = None, aoi: str | None = None, since: str | None = None
    ) -> list[Alert]:
        try:
            statuses = [AlertStatus(s) for s in status.split(",")] if status else None
        except ValueError as e:
            raise HTTPException(422, "invalid status") from e
        return st.list_alerts(since=_since(since), status=statuses, aoi=aoi)

    @app.get("/api/alerts/{alert_id}")
    def get_alert(alert_id: str) -> dict[str, Any]:
        a = need_alert(alert_id)
        return {
            "alert": a.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in st.get_evidence(a.evidence_ids)],
            "feedback": [f.model_dump(mode="json") for f in st.feedback_for(alert_id)],
            "actions": [_action_json(x) for x in st.actions_for(alert_id)],
            "playbook": playbook_for(a.kind.value),
        }

    @app.get("/api/evidence/{evidence_id}")
    def get_evidence(evidence_id: str) -> Any:
        ev = st.get_evidence([evidence_id])
        if not ev:
            raise HTTPException(404, "evidence not found")
        return ev[0]

    @app.post("/api/alerts/{alert_id}/feedback")
    def post_feedback(alert_id: str, body: FeedbackIn) -> Feedback:
        a = need_alert(alert_id)
        fb = Feedback(alert_id=alert_id, verdict=body.verdict, reason=body.reason, ts=_now())
        st.add_feedback(fb)
        status = AlertStatus.CONFIRMED if body.verdict == "confirm" else AlertStatus.DISMISSED
        st.upsert_alert(a.model_copy(update={"status": status}))
        return fb

    @app.post("/api/alerts/{alert_id}/actions")
    def propose_action(alert_id: str, body: ActionIn) -> dict[str, Any]:
        a = need_alert(alert_id)
        spec = pb.get(body.action_id)
        if spec is None:
            raise HTTPException(422, f"unknown action_id: {body.action_id}")
        applies = spec.get("applies_to", [])
        if "*" not in applies and a.kind.value not in applies:
            raise HTTPException(422, f"action {body.action_id} does not apply to {a.kind.value}")
        req = ActionRequest(
            alert_id=alert_id,
            action_id=body.action_id,
            id=uuid.uuid4().hex,
            ts=_now(),
        )
        st.upsert_action(req)
        return _action_json(req)

    def find_action(action_id: str) -> ActionRequest:
        x = st.get_action(action_id)
        if x is None or x.status != "proposed":
            raise HTTPException(404, "proposed action not found")
        return x

    @app.post("/api/actions/{action_id}/approve")
    def approve_action(action_id: str) -> dict[str, Any]:
        x = find_action(action_id)
        if x.action_id not in pb:  # defence in depth
            raise HTTPException(422, "action not in playbook")
        x.status = "approved"  # execution stub: records only, runs nothing
        x.result = {"executed": False, "note": "stub: approval recorded, no handler invoked"}
        st.upsert_action(x)
        return _action_json(x)

    @app.post("/api/actions/{action_id}/reject")
    def reject_action(action_id: str) -> dict[str, Any]:
        x = find_action(action_id)
        x.status = "rejected"
        st.upsert_action(x)
        return _action_json(x)

    # ------------------------------------------------------------------ map data
    @app.get("/api/cells")
    def cells(res: int = 4, since: str | None = None) -> list[dict[str, Any]]:
        start = _since(since, 24)
        latest: dict[str, Any] = {}
        for al in st.list_alerts(since=start, limit=1000):
            for ev in st.get_evidence(al.evidence_ids):
                if ev.h3_cell and (ev.h3_res in (None, res)):
                    cur = latest.get(ev.h3_cell)
                    if cur is None or ev.window_end > cur.window_end:
                        latest[ev.h3_cell] = ev
        return [
            {
                "h3": c,
                "res": res,
                "ratio": ev.features.get("degraded_ratio"),
                "score": ev.score,
                "kind": ev.kind.value,
                "n_observed": ev.coverage.n_observed,
                "n_quality": ev.coverage.n_quality,
                "ts": ev.window_end.isoformat(),
                "evidence_id": ev.id,
                "geometry": ev.geometry,
            }
            for c, ev in latest.items()
        ]

    @app.get("/api/tracks/{hex_id}")
    def tracks(hex_id: str, since: str | None = None) -> list[dict[str, Any]]:
        start = _since(since, 24) or _now()
        rows = st.aircraft_window(start, _now() + timedelta(minutes=1))
        h = hex_id.lower()
        pts = sorted((r for r in rows if r.hex == h), key=lambda r: r.ts)
        return [
            {"ts": r.ts.isoformat(), "lat": r.lat, "lon": r.lon, "alt": r.alt_baro, "gs": r.gs}
            for r in pts
        ]

    @app.get("/api/metrics/{key}/{metric}")
    def metrics(key: str, metric: str, since: str | None = None) -> list[dict[str, Any]]:
        start = _since(since, 24) or _now()
        return [{"ts": t.isoformat(), "value": v} for t, v in st.metric_history(key, metric, start)]

    @app.get("/api/aois")
    def aois() -> list[config.AOI]:
        return config.settings().aois

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "time": _now().isoformat(),
            "sources": [h.model_dump(mode="json") for h in st.list_health()],
        }

    @app.get("/api/ui-config")
    def ui_config() -> dict[str, str]:
        return {"basemap": basemap or config.settings().ui.basemap}

    @app.get("/api/playbook")
    def get_playbook() -> list[dict[str, Any]]:
        return [{"id": k, **v} for k, v in pb.items()]

    # ------------------------------------------------------------------ SSE
    async def alert_events(
        interval: float = stream_interval_s, max_ticks: int | None = None
    ) -> AsyncIterator[dict[str, str]]:
        """Snapshot of current alerts on connect, then one `alert` event per change."""
        seen: dict[str, tuple[datetime, str]] = {}
        first = True
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            current = await asyncio.to_thread(st.list_alerts, None, None, None, 500)
            changed = [a for a in current if seen.get(a.id) != (a.last_seen, a.status.value)]
            for a in changed:
                seen[a.id] = (a.last_seen, a.status.value)
            if first:
                yield {
                    "event": "snapshot",
                    "data": json.dumps([a.model_dump(mode="json") for a in current]),
                }
                first = False
            else:
                for a in changed:
                    yield {"event": "alert", "data": json.dumps(a.model_dump(mode="json"))}
            ticks += 1
            await asyncio.sleep(interval)

    app.state.alert_events = alert_events

    @app.get("/api/stream")
    async def stream() -> EventSourceResponse:
        return EventSourceResponse(alert_events(), ping=15)

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app
