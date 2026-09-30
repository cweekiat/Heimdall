"""FastAPI app for the analyst UI. Read-mostly; actions are limited to config/playbook.yaml ids."""

from __future__ import annotations

import asyncio
import json
import statistics
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from heimdall import config
from heimdall.adapters.aoi_file import MemoryAoiStore
from heimdall.aoi_ctl import AoiService
from heimdall.api import triage
from heimdall.api.aois import aois_router
from heimdall.api.basemap import basemap_router
from heimdall.api.keys import keys_router
from heimdall.api.layers import layers_router
from heimdall.api.traffic import traffic_router
from heimdall.live import LivePositions
from heimdall.ports import SecretStore, SourceControl, Store
from heimdall.schemas import (
    ActionRequest,
    Alert,
    AlertStatus,
    AnalystNote,
    Feedback,
)

WEB_DIST = config.ROOT / "web" / "dist"
LOCAL_HOSTS = ["127.0.0.1", "localhost"]


class FeedbackIn(BaseModel):
    verdict: Literal["confirm", "dismiss"]
    reason: str | None = Field(default=None, max_length=1000)


class StatusIn(BaseModel):
    status: AlertStatus
    reason: str | None = Field(default=None, max_length=1000)
    shelve_until: datetime | None = None


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class SourceIn(BaseModel):
    enabled: bool


class ActionIn(BaseModel):
    action_id: str


# Third-party hosts the web UI (web/src/basemap.ts) may contact, by configured provider. Vector
# styles + tiles + glyphs + sprites come from the provider host; the raster layers (satellite, EEZ,
# seamarks) are offered whenever a provider is set. "none" = fully private, nothing allowed.
_PROVIDER_HOSTS: dict[str, list[str]] = {
    "openfreemap": ["https://tiles.openfreemap.org"],
    "carto": ["https://basemaps.cartocdn.com", "https://tiles.basemaps.cartocdn.com"],
}
_RASTER_HOSTS = [
    "https://tiles.maps.eox.at",
    "https://geo.vliz.be",
    "https://tiles.openseamap.org",
]


def csp_for(provider: str) -> str:
    hosts = _PROVIDER_HOSTS.get(provider, []) + _RASTER_HOSTS if provider in _PROVIDER_HOSTS else []
    ext = " ".join(hosts)

    def join(*parts: str) -> str:
        return " ".join(p for p in parts if p)

    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            join("style-src 'self' 'unsafe-inline'", ext),
            join("img-src 'self' data: blob:", ext),
            join("font-src 'self' data:", ext),
            join("connect-src 'self'", ext),
            "worker-src 'self' blob:",
            "child-src blob:",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


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
    sources: SourceControl | None = None,
    secrets: SecretStore | None = None,
    aoi_service: AoiService | None = None,
    ui_extra: dict[str, Any] | None = None,
    live: LivePositions | None = None,
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
            if origin is not None:
                if urlsplit(origin).netloc != request.headers.get("host"):
                    return Response("cross-origin request rejected", status_code=403)
            elif request.headers.get("sec-fetch-site") != "same-origin":
                return Response("origin required", status_code=403)
        return await call_next(request)

    csp = csp_for(basemap or config.settings().ui.basemap)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[..., Awaitable[Response]]
    ) -> Response:
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = csp
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

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
            "notes": [n.model_dump(mode="json") for n in st.notes_for(alert_id)],
            "actions": [_action_json(x) for x in st.actions_for(alert_id)],
            "playbook": playbook_for(a.kind.value),
        }

    @app.get("/api/evidence/{evidence_id}")
    def get_evidence(evidence_id: str) -> Any:
        ev = st.get_evidence([evidence_id])
        if not ev:
            raise HTTPException(404, "evidence not found")
        return ev[0]

    def set_status(
        a: Alert, to: AlertStatus, reason: str | None, until: datetime | None
    ) -> AnalystNote:
        now = _now()
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        err = triage.validate(a.status, to, reason, until, now)
        if err:
            raise HTTPException(422, err)
        note = AnalystNote(
            id=uuid.uuid4().hex,
            alert_id=a.id,
            ts=now,
            kind="shelve" if to == AlertStatus.SHELVED else "status",
            text=(reason or "").strip(),
            status_to=to,
            shelve_until=until,
        )
        st.record_status_change(note)  # journal = audit trail; the store applies the status
        return note

    @app.post("/api/alerts/{alert_id}/status")
    def post_status(alert_id: str, body: StatusIn) -> dict[str, Any]:
        note = set_status(need_alert(alert_id), body.status, body.reason, body.shelve_until)
        return {
            "alert": need_alert(alert_id).model_dump(mode="json"),
            "note": note.model_dump(mode="json"),
        }

    @app.post("/api/alerts/clear")
    def clear_alerts() -> dict[str, int]:
        """Expire every open alert (not a verdict; DISMISSED is analyst-only), audited."""
        open_states = [
            AlertStatus.NEW,
            AlertStatus.ACKNOWLEDGED,
            AlertStatus.UNDER_REVIEW,
            AlertStatus.SHELVED,
        ]
        now = _now()
        n = 0
        for a in st.list_alerts(limit=1000, status=open_states):
            st.record_status_change(
                AnalystNote(
                    id=uuid.uuid4().hex,
                    alert_id=a.id,
                    ts=now,
                    kind="status",
                    text="cleared by user",
                    status_to=AlertStatus.EXPIRED,
                )
            )
            n += 1
        return {"cleared": n}

    @app.get("/api/alerts/{alert_id}/notes")
    def get_notes(alert_id: str) -> list[AnalystNote]:
        need_alert(alert_id)
        return st.notes_for(alert_id)

    @app.post("/api/alerts/{alert_id}/notes")
    def post_note(alert_id: str, body: NoteIn) -> AnalystNote:
        need_alert(alert_id)
        note = AnalystNote(
            id=uuid.uuid4().hex, alert_id=alert_id, ts=_now(), text=body.text.strip()
        )
        if not note.text:
            raise HTTPException(422, "note text is empty")
        st.add_note(note)
        return note

    @app.post("/api/alerts/{alert_id}/feedback")
    def post_feedback(alert_id: str, body: FeedbackIn) -> Feedback:
        a = need_alert(alert_id)
        to = AlertStatus.CONFIRMED if body.verdict == "confirm" else AlertStatus.DISMISSED
        set_status(a, to, body.reason, None)
        fb = Feedback(alert_id=alert_id, verdict=body.verdict, reason=body.reason, ts=_now())
        st.add_feedback(fb)
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
        h = hex_id.lower()
        # filter by hex in the store (indexed), not by scanning 24 h of all aircraft
        rows = st.aircraft_window(start, _now() + timedelta(minutes=1), ident=h)
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

    sev_rank = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    open_states = {AlertStatus.NEW, AlertStatus.ACKNOWLEDGED, AlertStatus.UNDER_REVIEW}

    @app.get("/api/aois/summary")
    def aois_summary() -> list[dict[str, Any]]:
        now = _now()
        alerts = st.list_alerts(limit=1000)
        out: list[dict[str, Any]] = []
        for aoi in config.settings().aois:
            al = [a for a in alerts if a.aoi == aoi.name]
            open_al = [a for a in al if a.status in open_states]
            evs = [e for a in al for e in st.get_evidence(a.evidence_ids)]
            latest: dict[str, Any] = {}
            for e in evs:
                if e.h3_cell and (
                    e.h3_cell not in latest or e.window_end > latest[e.h3_cell].window_end
                ):
                    latest[e.h3_cell] = e
            ratios = [
                e.features["degraded_ratio"]
                for e in latest.values()
                if "degraded_ratio" in e.features
            ]
            hist = [
                (t, v)
                for c in latest
                for t, v in st.metric_history(c, "degraded_ratio", now - timedelta(days=14))
            ]
            spark: list[float | None] = []
            for h in range(24):
                lo, hi = now - timedelta(hours=24 - h), now - timedelta(hours=23 - h)
                vals = [v for t, v in hist if lo <= t < hi]
                spark.append(round(sum(vals) / len(vals), 4) if vals else None)
            if ratios and spark[-1] is None:
                spark[-1] = round(max(ratios), 4)
            out.append(
                {
                    "name": aoi.name,
                    "label": aoi.label,
                    "role": aoi.role,
                    "lat": aoi.lat,
                    "lon": aoi.lon,
                    "radius_nm": aoi.radius_nm,
                    "origin": aoi.origin,
                    "polygon": aoi.polygon,
                    "open_total": len(open_al),
                    "open_by_kind": dict(Counter(a.kind.value for a in open_al)),
                    "max_severity": max(
                        (a.severity.value for a in open_al), key=lambda s: sev_rank[s], default=None
                    ),
                    "ratio_now": max(ratios) if ratios else None,
                    "ratio_median": round(statistics.median([v for _, v in hist]), 4)
                    if hist
                    else None,
                    "coverage": sum(e.coverage.n_observed for e in latest.values()),
                    "last_change": max((a.last_seen for a in al), default=None),
                    "spark": spark,
                }
            )
        return out

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "time": _now().isoformat(),
            "sources": [h.model_dump(mode="json") for h in st.list_health()],
        }

    # ------------------------------------------------------------------ sources
    @app.get("/api/sources")
    def list_sources() -> list[dict[str, Any]]:
        return [x.model_dump(mode="json") for x in sources.list()] if sources else []

    @app.post("/api/sources/{key}")
    async def set_source(
        body: SourceIn, key: str = Path(pattern=r"^[a-z0-9_]{1,40}$")
    ) -> dict[str, Any]:
        if sources is None:
            raise HTTPException(404, "source control unavailable")
        try:
            return (await sources.set_enabled(key, body.enabled)).model_dump(mode="json")
        except KeyError as e:
            raise HTTPException(404, "unknown source") from e

    # ------------------------------------------------------------------ actions board
    @app.get("/api/actions")
    def actions_board(
        status: str | None = None, since_h: float = Query(72, gt=0, le=720, allow_inf_nan=False)
    ) -> list[dict[str, Any]]:
        want = set(status.split(",")) if status else None
        out: list[dict[str, Any]] = []
        for al in st.list_alerts(since=_now() - timedelta(hours=since_h), limit=1000):
            for x in st.actions_for(al.id):
                if want is not None and x.status not in want:
                    continue
                meta = pb.get(x.action_id, {})
                out.append(
                    {
                        **_action_json(x),
                        "alert": {
                            "id": al.id,
                            "title": al.title,
                            "kind": al.kind.value,
                            "severity": al.severity.value,
                        },
                        "label": meta.get("label", x.action_id),
                        "risk": meta.get("risk", "low"),
                    }
                )
        return sorted(out, key=lambda a: a["ts"], reverse=True)

    app.include_router(traffic_router(st, index=live))
    app.include_router(layers_router(st))
    app.include_router(keys_router(secrets, sources))
    app.include_router(
        aois_router(
            aoi_service
            or AoiService(
                MemoryAoiStore(),  # demo/tests: user areas live for this process only
                config.settings(),
                sources,
                config.sources().get("sources", {}),
            )
        )
    )
    app.include_router(basemap_router())

    @app.get("/api/ui-config")
    def ui_config() -> dict[str, Any]:
        ui = config.settings().ui
        # ui_extra: optional start view / banner (e.g. a real-data replay), set by the host
        return {"basemap": basemap or ui.basemap, "overlays": ui.overlays, **(ui_extra or {})}

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
