"""User-drawn AOI routes (task 015). Mutations need a local client and a matching Origin.

Inputs are validated hard and never echoed back: every failure is a generic message.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from heimdall.aoi_ctl import AoiLimitError, AoiService
from heimdall.api.keys import _bad, _guard
from heimdall.schemas import MAX_USER_AOIS, UserAoiIn, UserAoiPatch

INVALID = "invalid area: check name, shape, radius or vertices"


def aois_router(svc: AoiService) -> APIRouter:
    r = APIRouter()

    @r.get("/api/aois/user")
    def list_user() -> list[dict[str, Any]]:
        return [u.model_dump(mode="json") for u in svc.list()]

    @r.post("/api/aois/user")
    async def create(request: Request) -> JSONResponse:
        if (denied := _guard(request)) is not None:
            return denied
        try:
            body = UserAoiIn.model_validate(await request.json())
            body.check()
        except (ValidationError, ValueError):
            return _bad(422, INVALID)
        try:
            u = await svc.create(body)
        except AoiLimitError:
            return _bad(409, f"at most {MAX_USER_AOIS} user areas")
        return JSONResponse(u.model_dump(mode="json"), status_code=201)

    @r.patch("/api/aois/user/{uid}")
    async def patch(uid: str, request: Request) -> JSONResponse:
        if (denied := _guard(request)) is not None:
            return denied
        try:
            body = UserAoiPatch.model_validate(await request.json())
        except (ValidationError, ValueError):
            return _bad(422, INVALID)
        u = await svc.update(uid, body)
        if u is None:
            return _bad(404, "unknown area")
        return JSONResponse(u.model_dump(mode="json"))

    @r.delete("/api/aois/user/{uid}")
    async def delete(uid: str, request: Request) -> JSONResponse:
        if (denied := _guard(request)) is not None:
            return denied
        if not await svc.delete(uid):
            return _bad(404, "unknown area")
        return JSONResponse({"deleted": True})

    return r
