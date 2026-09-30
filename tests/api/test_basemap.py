from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from heimdall.api.basemap import basemap_router

FIXTURE = Path(__file__).parents[1] / "fixtures" / "basemap" / "tiny.pmtiles"
SCRIPT = Path(__file__).parents[2] / "scripts" / "fetch_basemap.sh"


def _client(base: Path) -> TestClient:
    app = FastAPI()
    app.include_router(basemap_router(base))
    return TestClient(app)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    shutil.copy(FIXTURE, tmp_path / "world.pmtiles")
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "a.pbf").write_bytes(b"glyph")
    return _client(tmp_path)


def test_full_get(client: TestClient) -> None:
    r = client.get("/basemap/world.pmtiles")
    assert r.status_code == 200
    assert r.content == FIXTURE.read_bytes()
    assert r.headers["accept-ranges"] == "bytes"


def test_ranges(client: TestClient) -> None:
    data = FIXTURE.read_bytes()
    r = client.get("/basemap/world.pmtiles", headers={"Range": "bytes=0-15"})
    assert r.status_code == 206
    assert r.content == data[:16]
    assert r.headers["content-range"] == f"bytes 0-15/{len(data)}"
    r = client.get("/basemap/world.pmtiles", headers={"Range": "bytes=100-"})
    assert r.status_code == 206
    assert r.content == data[100:]
    r = client.get("/basemap/world.pmtiles", headers={"Range": "bytes=999999-"})
    assert r.status_code == 416
    r = client.get("/basemap/world.pmtiles", headers={"Range": "garbage"})
    assert r.status_code == 416


@pytest.mark.parametrize(
    "path",
    [
        "/basemap/../config/config.yaml",
        "/basemap/%2e%2e/config/config.yaml",
        "/basemap/..%2fconfig/config.yaml",
        "/basemap/%2Fetc/passwd",
        "/basemap//etc/passwd",
        "/basemap/fonts/../../x",
    ],
)
def test_traversal_rejected(client: TestClient, path: str) -> None:
    assert client.get(path).status_code in (400, 404)


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    base = tmp_path / "bm"
    base.mkdir()
    (base / "link").symlink_to(secret)
    assert _client(base).get("/basemap/link").status_code == 404


def test_style_local_only(client: TestClient) -> None:
    for theme in ("light", "dark"):
        r = client.get("/basemap/style.json", params={"theme": theme})
        assert r.status_code == 200
        assert "http://" not in r.text.replace("http://testserver", "")
        assert "https://" not in r.text
        assert r.json()["sources"]["protomaps"]["url"].startswith("pmtiles:///basemap/")


def test_status(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/api/basemap/status").json() == {"available": True}
    empty = tmp_path / "empty"
    empty.mkdir()
    c = _client(empty)
    body = c.get("/api/basemap/status").json()
    assert body["available"] is False
    assert "fetch_basemap.sh" in body["hint"]
    assert c.get("/basemap/world.pmtiles").status_code == 404


def test_fetch_script_syntax() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_bin_and_partial_files_not_served(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pmtiles").write_bytes(b"\x7fELF")
    (tmp_path / "world.pmtiles.part").write_bytes(b"x")
    c = _client(tmp_path)
    assert c.get("/basemap/bin/pmtiles").status_code == 404
    assert c.get("/basemap/world.pmtiles.part").status_code == 404


def test_huge_range_does_not_500(client: TestClient) -> None:
    r = client.get("/basemap/world.pmtiles", headers={"Range": "bytes=" + "9" * 5000 + "-"})
    assert r.status_code < 500
