"""ADR-008 spectrum connectors: WSPR, PSK Reporter, SatNOGS, Sentinel-1 RFI, SMAP (offline).

Fixtures were recorded from the live APIs on 2026-10-01 and trimmed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import h5py
import httpx
import numpy as np
import pytest
import respx

from heimdall.config import AOI
from heimdall.ingest.hf_common import band_of, maidenhead, midpoint
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.pskreporter import Aggregator, PskReporterConnector, parse_reports
from heimdall.ingest.satnogs import next_link, parse_observations, parse_stations
from heimdall.ingest.sentinel1_rfi import Sentinel1RfiConnector, parse_rfi, rfi_assets, to_event
from heimdall.ingest.smap import SmapConnector, parse_cmr, reduce_granule
from heimdall.ingest.wspr import WsprConnector, build_query, parse_wspr
from heimdall.schemas import Domain

FIX = Path(__file__).parent.parent / "fixtures"
NOW = datetime(2026, 9, 30, 16, 40, tzinfo=UTC)


def _json(p: str) -> Any:
    return json.loads((FIX / p).read_text())


# ---------------------------------------------------------------- HF helpers


def test_maidenhead_and_midpoint() -> None:
    lat, lon = maidenhead("JO31") or (0, 0)
    assert 51 < lat < 52 and 6 < lon < 8
    assert (
        maidenhead("FN01sk") is not None and maidenhead("ZZ99") is None and maidenhead("") is None
    )
    mlat, mlon = midpoint(0, 0, 0, 90)
    assert abs(mlat) < 1e-9 and abs(mlon - 45) < 1e-9
    assert abs(midpoint(0, 170, 0, -170)[1]) == pytest.approx(180, abs=1e-6)  # wraps
    assert band_of(14_075_706) == 14 and band_of(144e6) is None


# ---------------------------------------------------------------- WSPR


def test_wspr_query_and_parse() -> None:
    q = build_query(datetime(2026, 9, 30, 15, tzinfo=UTC), datetime(2026, 9, 30, 16, tzinfo=UTC))
    assert "'2026-09-30 15:00:00'" in q and "FORMAT JSON" in q and "distance >= 500" in q
    events, dropped = parse_wspr(_json("wspr/agg.json"))
    assert dropped == 0 and len(events) == 12
    e = events[0]
    assert e.kind == "hf_spots" and e.source == "wspr_live" and e.domain == Domain.SPECTRUM
    assert e.attrs["geo"] == "path_midpoint" and e.value == e.attrs["n"] and e.ts.tzinfo is not None
    assert len({x.id for x in events}) == 12
    assert parse_wspr({"data": [{"t": "bad"}]}) == ([], 1) and parse_wspr("x") == ([], 0)


def test_wspr_window_ends_on_whole_bins() -> None:
    c = WsprConnector("wspr_live", "https://w.invalid", now=lambda: NOW)
    t0, t1 = c.window()
    assert t1 == datetime(2026, 9, 30, 16, 30, tzinfo=UTC) and (t1 - t0).total_seconds() == 3600


@respx.mock
async def test_wspr_poll() -> None:
    respx.get("https://w.invalid/").mock(
        return_value=httpx.Response(200, json=_json("wspr/agg.json"))
    )
    c = WsprConnector(
        "wspr_live", "https://w.invalid", now=lambda: NOW, client=RateLimitedClient("t", rps=100)
    )
    assert len(await c.poll_once()) == 12 and c.health().status == "ok"


# ---------------------------------------------------------------- PSK Reporter


def test_psk_parse_and_aggregate() -> None:
    reports, dropped, seq = parse_reports((FIX / "pskreporter/query_ft8.xml").read_bytes())
    assert seq > 0 and len(reports) + dropped == 40 and reports
    agg = Aggregator(bbox=(-180, -90, 180, 90))
    touched, _ = agg.add(reports)
    evs = [agg.event(k) for k in touched]
    assert evs and all(e.kind == "hf_spots" and e.source == "pskreporter" for e in evs)
    assert sum(e.attrs["n"] for e in evs) == sum(b.n for b in agg.bins.values())
    # a second poll adds to the same bins instead of replacing them
    n0 = sum(b.n for b in agg.bins.values())
    agg.add(reports)
    assert sum(b.n for b in agg.bins.values()) == 2 * n0


def test_psk_rejects_entities_and_callsigns_not_stored() -> None:
    with pytest.raises(ValueError):
        parse_reports(b'<!DOCTYPE x [<!ENTITY a "b">]><receptionReports/>')
    reports, _, _ = parse_reports((FIX / "pskreporter/query_ft8.xml").read_bytes())
    agg = Aggregator(bbox=(-180, -90, 180, 90))
    touched, _ = agg.add(reports)
    blob = json.dumps([agg.event(k).model_dump(mode="json") for k in touched])
    assert "KJ5ROG" not in blob and "NR9H" not in blob


@respx.mock
async def test_psk_poll_uses_lastseqno() -> None:
    body = (FIX / "pskreporter/query_ft8.xml").read_bytes()
    route = respx.get("https://p.invalid/query").mock(
        return_value=httpx.Response(200, content=body)
    )
    c = PskReporterConnector(
        "pskreporter",
        "https://p.invalid",
        agg=Aggregator(bbox=(-180, -90, 180, 90)),
        now=lambda: NOW,
        client=RateLimitedClient("t", rps=100),
    )
    await c.poll_once()
    await c.poll_once()
    assert "lastseqno" not in str(route.calls[0].request.url)
    assert "lastseqno=73255257027" in str(route.calls[1].request.url)


# ---------------------------------------------------------------- SatNOGS


def test_satnogs_parse() -> None:
    obs = _json("satnogs/observations.json")
    stations = {o["ground_station"]: (o["station_lat"], o["station_lng"]) for o in obs}
    for o in obs:
        o["status"] = "bad"
    events, dropped = parse_observations(obs, stations)
    assert dropped == 0 and len(events) == len(obs)
    e = events[0]
    assert (
        e.kind == "sat_observation"
        and e.value == 0.0
        and e.attrs["rf_band"] in ("VHF", "UHF", "L", "S")
    )
    assert "observer" not in e.attrs and "station_name" not in e.attrs
    assert parse_observations(obs, {})[0] == []  # other region
    obs[0]["status"] = "future"
    assert len(parse_observations(obs, stations)[0]) == len(obs) - 1


def test_satnogs_stations_and_link() -> None:
    st = _json("satnogs/stations.json")
    assert parse_stations(st, (-180, -90, 180, 90), NOW, active_days=100000)
    assert parse_stations(st, (0, 0, 0.1, 0.1), NOW) == {}
    assert next_link('<https://n.invalid/api/observations/?cursor=x>; rel="next"') == (
        "https://n.invalid/api/observations/?cursor=x"
    )
    assert next_link(None) is None


# ---------------------------------------------------------------- Sentinel-1 RFI


def test_s1_rfi_parse() -> None:
    s = parse_rfi((FIX / "sentinel1_rfi/rfi_vv.xml").read_bytes())
    assert s["pol"] == "VV" and s["n_noise_reports"] == 17 and s["n_noise_rfi"] == 0
    assert s["n_bursts"] == 30 and s["max_power_ratio"] and s["start"].tzinfo is not None
    with pytest.raises(ValueError):
        parse_rfi(b"<product/>")
    with pytest.raises(ValueError):
        parse_rfi(b'<!DOCTYPE r [<!ENTITY x "y">]><rfi/>')


def test_s1_assets_only_rfi_xml_on_allowed_host() -> None:
    item = _json("sentinel1_rfi/items.json")["features"][0]
    hrefs = rfi_assets(item)
    assert set(hrefs) <= {"schema-product-vv", "schema-product-vh"} and hrefs
    assert all(".blob.core.windows.net/" in h and "?" not in h for h in hrefs.values())
    assert rfi_assets(item, hosts=(".example.org",)) == {}
    ev = to_event(item, [parse_rfi((FIX / "sentinel1_rfi/rfi_vv.xml").read_bytes())])
    assert ev.kind == "sar_rfi" and ev.geometry and ev.attrs["rfi_detected"] is False


@respx.mock
async def test_s1_poll_fetches_only_rfi_xml() -> None:
    items = _json("sentinel1_rfi/items.json")
    respx.get(url__regex=r"https://pc\.invalid/collections/.*").mock(
        return_value=httpx.Response(200, json=items)
    )
    respx.get("https://pc.invalid/sas").mock(
        return_value=httpx.Response(
            200, json={"token": "st=x&sig=y", "msft:expiry": "2099-01-01T00:00:00Z"}
        )
    )
    xml = respx.get(url__regex=r"https://[a-z0-9]+\.blob\.core\.windows\.net/.*").mock(
        return_value=httpx.Response(200, content=(FIX / "sentinel1_rfi/rfi_vv.xml").read_bytes())
    )
    c = Sentinel1RfiConnector(
        "sentinel1_rfi",
        "https://pc.invalid",
        [AOI(name="t", lat=1.3, lon=103.8, radius_nm=100)],
        sas_url="https://pc.invalid/sas",
        now=lambda: NOW,
        client=RateLimitedClient("t", rps=100),
    )
    evs = await c.poll_once()
    assert len(evs) == 1 and xml.call_count == 2
    assert all("/annotation/rfi/rfi-" in str(r.request.url) for r in xml.calls)
    assert await c.poll_once() == []  # scene already done


# ---------------------------------------------------------------- SMAP


def _smap_file(path: Path) -> None:
    lat = np.array([[1.0, 1.01, 1.02, 30.0], [1.0, 1.01, -9999.0, 30.0]])
    lon = np.array([[104.0, 104.01, 104.02, 0.0], [104.0, 104.01, -9999.0, 0.0]])
    qh = np.array([[4, 0, 0, 4], [4, 0, 0, 0]], dtype=np.uint16)  # bit 2 = RFI
    with h5py.File(path, "w") as f:
        g = f.create_group("Brightness_Temperature")
        g["tb_lat"], g["tb_lon"], g["tb_h"] = lat, lon, np.full(lat.shape, 250.0)
        g["tb_qual_flag_h"], g["tb_qual_flag_v"] = qh, np.zeros_like(qh)


def test_smap_reduce(tmp_path: Path) -> None:
    p = tmp_path / "g.h5"
    _smap_file(p)
    evs = reduce_granule(p, "G1", NOW, bbox=(60, -20, 160, 50), h3_res=2, min_footprints=1)
    assert len(evs) == 1  # the lon-0 footprints are outside bbox, the fill value is ignored
    e = evs[0]
    assert (
        e.attrs["n_footprints"] == 5 and e.attrs["n_rfi_h"] == 2 and e.value == pytest.approx(0.4)
    )
    assert e.kind == "l_band_rfi" and e.attrs["mask_h"] == 4


def test_smap_cmr_parse_keeps_allowed_https_h5() -> None:
    payload = {
        "feed": {
            "entry": [
                {
                    "title": "A",
                    "time_start": "2026-09-30T13:34:44Z",
                    "links": [{"href": "https://data.nsidc.earthdatacloud.nasa.gov/x/A.h5"}],
                },
                {
                    "title": "B",
                    "time_start": "2026-09-30T13:34:44Z",
                    "links": [{"href": "https://evil.example/B.h5"}],
                },
            ]
        }
    }
    assert [t for t, _, _ in parse_cmr(payload, ("data.nsidc.earthdatacloud.nasa.gov",))] == ["A"]


async def test_smap_needs_key_makes_no_request() -> None:
    c = SmapConnector("smap_rfi", "https://cmr.invalid", token=None)
    assert c.health().status == "needs_key" and await c.poll_once() == []
