"""User-AOI service (task 015): validate -> persist -> update runtime settings -> restart pollers.

Business rules only; persistence is behind `ports.AoiStore`, source control behind
`ports.SourceControl`. Detectors and fusion read `settings().aois` on their next tick.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Mapping
from typing import Any

from heimdall.config import AOI, Settings
from heimdall.geo import KM_PER_NM, haversine_km
from heimdall.logging import get_logger
from heimdall.ports import AoiStore, SourceControl
from heimdall.schemas import MAX_USER_AOIS, UserAoi, UserAoiIn, UserAoiPatch

log = get_logger("heimdall.aoi_ctl")


class AoiLimitError(Exception):
    pass


def bounding_circle(ring: list[tuple[float, float]]) -> tuple[float, float, float]:
    """(lat, lon, radius_nm) of a circle centred on the bbox centre that covers every vertex."""
    lat = (min(p[0] for p in ring) + max(p[0] for p in ring)) / 2
    lon = (min(p[1] for p in ring) + max(p[1] for p in ring)) / 2
    r = max(haversine_km(lat, lon, a, b) for a, b in ring) / KM_PER_NM
    return round(lat, 5), round(lon, 5), max(5.0, math.ceil(r * 10) / 10)


def to_runtime(u: UserAoi) -> AOI:
    return AOI(
        name=u.id,
        label=u.name,
        role=u.role,
        lat=u.lat,
        lon=u.lon,
        radius_nm=u.radius_nm,
        origin="user",
        polygon=u.polygon,
    )


class AoiService:
    def __init__(
        self,
        store: AoiStore,
        settings: Settings,
        sources: SourceControl | None = None,
        source_cfg: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._sources = sources
        # sources whose connectors take `settings.aois` at build time
        self._aoi_sources = [k for k, c in (source_cfg or {}).items() if c.get("uses_aois")]
        self._rows = store.load()
        self._apply_settings()

    def list(self) -> list[UserAoi]:
        return list(self._rows)

    def _apply_settings(self) -> None:
        self._settings.set_user_aois([to_runtime(u) for u in self._rows if u.active])

    async def _commit(self) -> None:
        self._store.save(self._rows)
        self._apply_settings()
        if self._sources is None:
            return
        for key in self._aoi_sources:
            try:
                await self._sources.restart(key)
            except KeyError:
                continue
            except Exception as e:  # a failed restart must not fail the edit
                log.error("aoi_restart_failed", source=key, error=type(e).__name__)

    async def create(self, body: UserAoiIn) -> UserAoi:
        body.check()
        if len(self._rows) >= MAX_USER_AOIS:
            raise AoiLimitError
        if body.shape == "circle":
            assert body.lat is not None and body.lon is not None and body.radius_nm is not None
            lat, lon, rad = body.lat, body.lon, body.radius_nm
            ring = None
        else:
            assert body.polygon is not None
            ring = [(float(a), float(b)) for a, b in body.polygon]
            lat, lon, rad = bounding_circle(ring)
        taken = {u.id for u in self._rows}
        while (uid := "user_" + secrets.token_hex(3)) in taken:
            pass
        u = UserAoi(
            id=uid,
            name=body.name,
            role=body.role,
            active=body.active,
            shape=body.shape,
            lat=lat,
            lon=lon,
            radius_nm=rad,
            polygon=ring,
        )
        self._rows.append(u)
        await self._commit()
        return u

    async def update(self, uid: str, patch: UserAoiPatch) -> UserAoi | None:
        for i, u in enumerate(self._rows):
            if u.id == uid:
                new = u.model_copy(update=patch.model_dump(exclude_none=True))
                self._rows[i] = new
                await self._commit()
                return new
        return None

    async def delete(self, uid: str) -> bool:
        keep = [u for u in self._rows if u.id != uid]
        if len(keep) == len(self._rows):
            return False
        self._rows = keep
        await self._commit()
        return True
