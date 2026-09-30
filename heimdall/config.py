"""Typed config loader. Values come from config/*.yaml; secrets from environment (.env)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from heimdall.geo import point_in_polygon, within_radius_nm

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("HEIMDALL_CONFIG_DIR", ROOT / "config"))


class AOI(BaseModel):
    name: str
    role: Literal["primary", "control"] = "primary"
    lat: float
    lon: float
    radius_nm: float
    label: str | None = None  # display name for the UI
    # per-AOI detector overrides merged over `detectors` (e.g. a sparse-traffic jamming profile)
    detectors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # task 015: user-drawn areas. A polygon AOI keeps its bounding circle in lat/lon/radius_nm
    # (what connectors poll) and the exact ring here as (lat, lon) vertices for filtering.
    origin: Literal["config", "user"] = "config"
    polygon: list[tuple[float, float]] | None = None

    def contains(self, lat: float, lon: float) -> bool:
        """Is the point inside this AOI (exact polygon if drawn, else the circle)?"""
        if not within_radius_nm(lat, lon, self.lat, self.lon, self.radius_nm):
            return False
        return self.polygon is None or point_in_polygon(lat, lon, self.polygon)


class StorageCfg(BaseModel):
    duckdb_path: str = "data/heimdall.duckdb"
    lake_dir: str = "data/lake"
    bus: str = "memory"
    store: str = "duckdb"


class ApiCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    auth: str = "none"


class UiCfg(BaseModel):
    # none = fully private (no third-party requests); openfreemap|carto = third-party vector tiles.
    basemap: Literal["openfreemap", "carto", "none"] = "openfreemap"
    overlays: list[str] = Field(default_factory=lambda: ["borders"])


class Settings(BaseModel):
    storage: StorageCfg = Field(default_factory=StorageCfg)
    api: ApiCfg = Field(default_factory=ApiCfg)
    ui: UiCfg = Field(default_factory=UiCfg)
    aois: list[AOI] = Field(default_factory=list)
    detectors: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def set_user_aois(self, user: list[AOI]) -> None:
        """Replace the user-drawn (active) AOIs; config AOIs are never touched."""
        self.aois = [a for a in self.aois if a.origin == "config"] + list(user)

    def aoi(self, name: str) -> AOI:
        return next(a for a in self.aois if a.name == name)

    def detectors_for(self, aoi: AOI) -> dict[str, dict[str, Any]]:
        """Global detector config with the AOI's per-detector overrides merged on top."""
        out = {k: dict(v) for k, v in self.detectors.items()}
        for k, v in aoi.detectors.items():
            out[k] = {**out.get(k, {}), **v}
        return out


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    assert isinstance(data, dict)
    return data


@lru_cache(maxsize=1)
def settings() -> Settings:
    s = Settings.model_validate(_load_yaml("config.yaml"))
    if host := os.environ.get("HEIMDALL_API_HOST"):
        s.api.host = host  # e.g. 0.0.0.0 inside a container; publish on 127.0.0.1 only
    return s


def sources() -> dict[str, Any]:
    return _load_yaml("sources.yaml")


def playbook() -> dict[str, Any]:
    return _load_yaml("playbook.yaml")


def secret(name: str) -> str | None:
    """Read a secret from the SecretStore (data/secrets), falling back to env (ADR-006)."""
    from heimdall.adapters.secrets_file import default_store

    return default_store().get(name)
