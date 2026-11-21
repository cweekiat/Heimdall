"""Write-only API-key routes. Values are never returned, echoed or logged."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from heimdall.logging import get_logger
from heimdall.ports import SecretStore, SourceControl
from heimdall.schemas import KEY_CONNECTORS, key_connectors

log = get_logger("heimdall.api.keys")

LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost"}


class KeyIn(BaseModel):
    # 4096: NASA Earthdata tokens are JWTs of ~1 kB (a 512 cap silently rejected them).
    value: str = Field(min_length=1, max_length=4096, pattern=r"^[\x21-\x7e]+$")


def _bad(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"detail": msg}, status_code=status)


def _guard(request: Request) -> JSONResponse | None:
    client = request.client.host if request.client else None
    if client not in LOCAL_CLIENTS:
        return _bad(403, "local client required")
    origin = request.headers.get("origin")
    if origin is None or urlsplit(origin).netloc != request.headers.get("host"):
        return _bad(403, "matching origin required")
    return None


def keys_router(secrets: SecretStore | None, sources: SourceControl | None) -> APIRouter:
    r = APIRouter()

    @r.get("/api/keys")
    def list_keys() -> list[dict[str, Any]]:
        return [k.model_dump(mode="json") for k in secrets.status()] if secrets else []

    @r.put("/api/keys/{name}")
    async def put_key(name: str, request: Request) -> JSONResponse:
        if secrets is None or name not in KEY_CONNECTORS:
            return _bad(404, "unknown key")
        if (denied := _guard(request)) is not None:
            return denied
        try:
            body = KeyIn.model_validate(await request.json())
        except ValidationError as e:
            kind = e.errors()[0].get("type", "") if e.errors() else ""
            msg = {
                "string_too_long": "key too long (max 4096 characters)",
                "string_pattern_mismatch": "key has spaces or non-printable characters",
            }.get(kind, "invalid key value")
            return _bad(422, msg)
        except ValueError:
            return _bad(422, "invalid key value")
        secrets.set(name, body.value)
        for conn in key_connectors(name):
            await _restart(sources, conn)
        return JSONResponse({"name": name, "saved": True})

    @r.delete("/api/keys/{name}")
    async def delete_key(name: str, request: Request) -> JSONResponse:
        if secrets is None or name not in KEY_CONNECTORS:
            return _bad(404, "unknown key")
        if (denied := _guard(request)) is not None:
            return denied
        secrets.delete(name)
        for conn in key_connectors(name):
            await _restart(sources, conn)
        return JSONResponse({"name": name, "deleted": True})

    return r


async def _restart(sources: SourceControl | None, connector: str) -> None:
    if sources is None:
        return
    try:
        await sources.restart(connector)
    except KeyError:
        log.warning("key_restart_unknown_source", source=connector)
    except Exception as e:
        log.error("key_restart_failed", source=connector, error=type(e).__name__)
