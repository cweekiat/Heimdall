"""049: global context - OSM military bases, GDELT conflict events, news headlines."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heimdall.adapters.memory_store import MemoryStore
from heimdall.api.app import create_app
from heimdall.api.military import _mark_base
from heimdall.ingest import gdelt, news_rss, osm_bases

FIX = Path(__file__).parents[1] / "fixtures"
CSV_HEAD = "@type\t@id\t@lat\t@lon\tmilitary\tname:en\tname\toperator\ticao"


def test_osm_bases_csv_parse_and_near() -> None:
    text = "\n".join(
        [
            CSV_HEAD,
            "node\t1\t52.4\t0.56\tairfield\t\tRAF Lakenheath\tUSAF\tEGUL",
            "way\t2\t36.6\t-6.35\tnaval_base\tNaval Station Rota\tBase Naval de Rota\t\t",
            "node\t3\t10\t10\tbarracks\t\tIgnored\t\t",  # not a kept kind
            "node\t4\t\t\tbase\t\tNo position\t\t",
        ]
    )
    rows = osm_bases.parse_csv(text)
    assert [r["name"] for r in rows] == ["RAF Lakenheath", "Naval Station Rota"]
    assert rows[0]["id"] == "n1" and rows[1]["kind"] == "naval_base" and rows[0]["icao"] == "EGUL"
    assert osm_bases.near(52.41, 0.57, 3.0, rows)[0]["name"] == "RAF Lakenheath"
    assert osm_bases.parse_csv("<html>busy</html>") == []


def test_bases_endpoint_and_airfield_marked_military() -> None:
    Path(os.environ["HEIMDALL_BASES"]).write_text(
        json.dumps(
            [{"id": "n1", "name": "RAF Lakenheath", "kind": "airfield", "lat": 52.4, "lon": 0.56}]
        )
    )
    c = TestClient(create_app(MemoryStore(), allowed_hosts=["testserver"]))
    fc = c.get("/api/military/bases?bbox=-10,40,10,60").json()
    assert fc["total"] == 1 and fc["features"][0]["properties"]["label"] == "Air base"
    assert c.get("/api/military/bases?bbox=100,0,120,10").json()["total"] == 0
    assert c.get("/api/military/bases?bbox=x").status_code == 422
    dep = _mark_base({"airfield": {"lat": 52.405, "lon": 0.565, "military": False}})
    assert dep["airfield"]["military"] is True and dep["airfield"]["base"] == "RAF Lakenheath"


def test_gdelt_export_keeps_located_conflict_events() -> None:
    text = (FIX / "gdelt" / "export_sample.CSV").read_text(encoding="utf-8", errors="replace")
    evs, _ = gdelt.parse_export(text)
    assert evs and all(e.kind == "conflict_event" and e.attrs["root"] in gdelt.ROOTS for e in evs)
    assert all(e.lat is not None and e.attrs["precision"] != "country" for e in evs)
    assert all(e.url is None or e.url.startswith("http") for e in evs)
    lu = "1 a http://data.gdeltproject.org/gdeltv2/20261003080000.export.CSV.zip\n"
    assert (
        gdelt.export_url(lu)
        == "https://data.gdeltproject.org/gdeltv2/20261003080000.export.CSV.zip"
    )
    assert gdelt.export_url("1 a https://evil.example/x.export.CSV.zip") is None


def test_conflict_layer_serves_whitelisted_fields() -> None:
    s = MemoryStore()
    evs, _ = gdelt.parse_export((FIX / "gdelt" / "export_sample.CSV").read_text(errors="replace"))
    now = datetime.now(UTC)
    s.insert_events([e.model_copy(update={"ts": now - timedelta(minutes=5)}) for e in evs])
    c = TestClient(create_app(s, allowed_hosts=["testserver"]))
    fc = c.get("/api/layers/conflict?aoi=all&hours=24").json()
    p = fc["features"][0]["properties"]
    assert fc["total"] == len(evs) and "label" in p and "sources" in p and "actor1" not in p


def test_news_rss_parse_guard_and_endpoint() -> None:
    now = datetime.now(UTC)
    evs, _ = news_rss.parse_rss((FIX / "news" / "bbc_world.xml").read_bytes(), "BBC World", now)
    assert len(evs) > 10 and all(e.url and e.url.startswith("https://") for e in evs)
    with pytest.raises(ValueError):
        news_rss.parse_rss(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss/>', "x", now)
    s = MemoryStore()
    dup = evs[0].model_copy(update={"id": "news|dup"})
    s.insert_events([e.model_copy(update={"ts": now - timedelta(hours=1)}) for e in [*evs, dup]])
    c = TestClient(create_app(s, allowed_hosts=["testserver"]))
    d = c.get("/api/news?limit=5").json()
    assert len(d["items"]) == 5 and d["total"] == len(evs) and d["items"][0]["untrusted"] is True
