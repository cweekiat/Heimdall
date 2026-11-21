"""Offline basemap serving: local PMTiles, fonts, sprites, with HTTP Range support."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

BASEMAP_DIR = Path(__file__).resolve().parents[2] / "data" / "basemap"
HINT = "run scripts/fetch_basemap.sh"
_RANGE = re.compile(r"^bytes=(\d{0,18})-(\d{0,18})$")
_SERVABLE = {".pmtiles", ".json", ".png", ".pbf"}
_CHUNK = 64 * 1024

_THEMES = {
    "light": {"bg": "#e8e6e0", "water": "#a8c4dc", "line": "#8a8a8a", "text": "#333333"},
    "dark": {"bg": "#14181d", "water": "#0b1a2a", "line": "#4a5560", "text": "#b8c2cc"},
}


def _resolve(base: Path, rel: str) -> Path | None:
    """Confine `rel` to `base`; reject traversal, absolute paths and escaping symlinks."""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return None
    if any(part in ("..", "") for part in rel.split("/")):
        return None
    root = base.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    if target.suffix not in _SERVABLE or target.relative_to(root).parts[0] == "bin":
        return None
    return target


def _iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    with path.open("rb") as f:
        f.seek(start)
        left = end - start + 1
        while left > 0:
            chunk = f.read(min(_CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk


def _media_type(path: Path) -> str:
    return {
        ".json": "application/json",
        ".png": "image/png",
        ".pbf": "application/x-protobuf",
    }.get(path.suffix, "application/octet-stream")


def _serve(path: Path, request: Request) -> Response:
    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=3600"}
    media = _media_type(path)
    rng = request.headers.get("range")
    if rng is None:
        if size == 0:
            return Response(b"", media_type=media, headers=headers)
        headers["Content-Length"] = str(size)
        return StreamingResponse(_iter_file(path, 0, size - 1), media_type=media, headers=headers)
    m = _RANGE.match(rng.strip())
    bad = Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return bad
    if m.group(1) == "":  # suffix range: last N bytes
        n = int(m.group(2))
        if n == 0 or size == 0:
            return bad
        start, end = max(size - n, 0), size - 1
    else:
        start = int(m.group(1))
        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if start >= size or start > end:
        return bad
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(
        _iter_file(path, start, end), status_code=206, media_type=media, headers=headers
    )


OVERVIEW = "overview.pmtiles"  # optional whole-world low-zoom file (fetch_basemap.sh, WORLD=1)
# region covered by world.pmtiles (fetch_basemap.sh BBOX); overview labels are hidden inside it so
# names are not drawn twice
REGION_BBOX = (32.0, -12.0, 145.0, 42.0)


def _layers(src: str, c: dict[str, str], suffix: str, outside: list[Any] | None) -> list[Any]:
    def f(extra: list[Any] | None) -> dict[str, Any]:
        return {"filter": extra} if extra else {}

    return [
        {
            "id": f"earth{suffix}",
            "type": "fill",
            "source": src,
            "source-layer": "earth",
            "paint": {"fill-color": c["bg"]},
        },
        {
            "id": f"water{suffix}",
            "type": "fill",
            "source": src,
            "source-layer": "water",
            "paint": {"fill-color": c["water"]},
        },
        {
            "id": f"boundaries{suffix}",
            "type": "line",
            "source": src,
            "source-layer": "boundaries",
            "paint": {"line-color": c["line"], "line-width": 0.8, "line-dasharray": [3, 2]},
        },
        {
            "id": f"roads{suffix}",
            "type": "line",
            "source": src,
            "source-layer": "roads",
            "minzoom": 6,
            "paint": {"line-color": c["line"], "line-width": 0.5},
            **f(outside),
        },
        {
            "id": f"places{suffix}",
            "type": "symbol",
            "source": src,
            "source-layer": "places",
            "layout": {
                "text-field": ["get", "name"],
                "text-font": ["Noto Sans Regular"],
                "text-size": 11,
            },
            "paint": {"text-color": c["text"]},
            **f(outside),
        },
    ]


def build_style(theme: str = "light", overview: bool = False) -> dict[str, Any]:
    """Offline style. With `overview` (a whole-world z0-6 file) the world is drawn first and the
    detailed regional file on top, so the globe and areas outside the region are not blank."""
    c = _THEMES.get(theme, _THEMES["light"])
    src = "protomaps"
    sources: dict[str, Any] = {src: {"type": "vector", "url": "pmtiles:///basemap/world.pmtiles"}}
    # areas with no tiles at all look like open water rather than land without coastlines
    layers: list[Any] = [
        {"id": "background", "type": "background", "paint": {"background-color": c["water"]}}
    ]
    if overview:
        sources["world"] = {"type": "vector", "url": f"pmtiles:///basemap/{OVERVIEW}"}
        w, s_, e, n = REGION_BBOX
        ring = [[w, s_], [e, s_], [e, n], [w, n], [w, s_]]
        outside = ["!", ["within", {"type": "Polygon", "coordinates": [ring]}]]
        layers += _layers("world", c, "-world", outside)
    layers += _layers(src, c, "", None)
    return {
        "version": 8,
        "name": f"heimdall-{theme}",
        "glyphs": "/basemap/fonts/{fontstack}/{range}.pbf",
        "sprite": f"/basemap/sprites/v4/{theme}",
        "sources": sources,
        "layers": layers,
    }


def basemap_router(base_dir: Path | None = None) -> APIRouter:
    router = APIRouter()

    def base() -> Path:
        return base_dir if base_dir is not None else BASEMAP_DIR

    @router.get("/api/basemap/status")
    def status() -> dict[str, Any]:
        if (base() / "world.pmtiles").is_file():
            return {"available": True}
        return {"available": False, "hint": HINT}

    @router.get("/basemap/style.json")
    def style(theme: str = "light") -> JSONResponse:
        return JSONResponse(
            build_style(
                theme if theme in _THEMES else "light",
                overview=(base() / OVERVIEW).is_file(),
            )
        )

    @router.api_route("/basemap/{file:path}", methods=["GET", "HEAD"])
    def serve(file: str, request: Request) -> Response:
        path = _resolve(base(), file)
        if path is None:
            return JSONResponse({"detail": "not found", "hint": HINT}, status_code=404)
        return _serve(path, request)

    return router
