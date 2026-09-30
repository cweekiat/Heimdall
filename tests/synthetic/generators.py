"""Synthetic ADS-B/MLAT generators for detector tests. Deterministic (seeded), no network.

All generators return `list[AircraftObs]` with h3_r4/h3_r6 filled like the ingest layer does.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from heimdall.geo import EARTH_RADIUS_KM, KM_PER_NM, h3_cell, h3_center
from heimdall.schemas import AircraftObs

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
PROVIDERS = ("adsb.lol", "adsb.fi", "airplanes.live")


def destination(lat: float, lon: float, bearing_deg: float, dist_km: float) -> tuple[float, float]:
    """Great-circle destination point."""
    d = dist_km / EARTH_RADIUS_KM
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def cell_center(lat: float, lon: float, res: int = 4) -> tuple[float, float]:
    return h3_center(h3_cell(lat, lon, res))


def make_obs(hex_: str, ts: datetime, lat: float, lon: float, **kw: Any) -> AircraftObs:
    base: dict[str, Any] = {
        "provider": PROVIDERS[0],
        "src_type": "adsb_icao",
        "alt_baro": 30000,
        "alt_geom": 30100,
        "gs": 450.0,
        "nic": 8,
        "nac_p": 9,
    }
    base.update(kw)
    return AircraftObs(
        ts=ts,
        hex=hex_,
        lat=lat,
        lon=lon,
        h3_r4=h3_cell(lat, lon, 4),
        h3_r6=h3_cell(lat, lon, 6),
        **base,
    )


def straight_flight(
    hex_: str = "a00001",
    start: datetime = T0,
    lat: float = 20.0,
    lon: float = 96.0,
    heading: float = 45.0,
    speed_kt: float = 450.0,
    minutes: float = 75.0,
    dt_s: float = 10.0,
    **kw: Any,
) -> list[AircraftObs]:
    out: list[AircraftObs] = []
    n = int(minutes * 60 / dt_s)
    for i in range(n):
        dist = speed_kt * KM_PER_NM * (i * dt_s) / 3600.0
        la, lo = destination(lat, lon, heading, dist)
        out.append(
            make_obs(
                hex_, start + timedelta(seconds=i * dt_s), la, lo, gs=speed_kt, track=heading, **kw
            )
        )
    return out


def _path_obs(
    hex_: str,
    start: datetime,
    lat0: float,
    lon0: float,
    xy_track: Sequence[tuple[float, float, float]],
    dt_s: float,
    speed_kt: float,
    **kw: Any,
) -> list[AircraftObs]:
    """xy_track: (x_km east, y_km north, track_deg) samples at dt_s cadence around (lat0, lon0)."""
    out = []
    for i, (x, y, trk) in enumerate(xy_track):
        dist = math.hypot(x, y)
        brg = math.degrees(math.atan2(x, y)) % 360.0
        la, lo = destination(lat0, lon0, brg, dist) if dist > 0 else (lat0, lon0)
        out.append(
            make_obs(
                hex_,
                start + timedelta(seconds=i * dt_s),
                la,
                lo,
                gs=speed_kt,
                track=trk % 360,
                **kw,
            )
        )
    return out


def racetrack(
    hex_: str = "ae0001",
    start: datetime = T0,
    lat: float = 20.0,
    lon: float = 96.0,
    leg_nm: float = 60.0,
    axis_deg: float = 90.0,
    speed_kt: float = 420.0,
    turn_rate_dps: float = 1.5,
    minutes: float = 75.0,
    dt_s: float = 10.0,
    type_code: str = "K35R",
    mil: bool = True,
    **kw: Any,
) -> list[AircraftObs]:
    """Racetrack orbit: two straight legs of `leg_nm` joined by 180-degree standard turns."""
    v = speed_kt * KM_PER_NM / 3600.0  # km/s
    leg = leg_nm * KM_PER_NM
    r = v / math.radians(turn_rate_dps)  # turn radius km
    lap = 2 * leg + 2 * math.pi * r
    ax = math.radians(axis_deg)
    ux, uy = math.sin(ax), math.cos(ax)  # along-axis unit (east, north)
    px, py = -uy, ux  # left-perpendicular
    samples = []
    for i in range(int(minutes * 60 / dt_s)):
        s = (v * i * dt_s) % lap
        if s < leg:  # outbound leg along axis, offset -r
            a, p, trk = s - leg / 2, -r, axis_deg
        elif s < leg + math.pi * r:  # turn left through 180
            th = (s - leg) / r
            a, p, trk = leg / 2 + r * math.sin(th), -r * math.cos(th), axis_deg - math.degrees(th)
        elif s < 2 * leg + math.pi * r:
            a, p, trk = leg / 2 - (s - leg - math.pi * r), r, axis_deg + 180
        else:
            th = (s - 2 * leg - math.pi * r) / r
            a, p, trk = (
                -leg / 2 - r * math.sin(th),
                r * math.cos(th),
                axis_deg + 180 - math.degrees(th),
            )
        samples.append((a * ux + p * px, a * uy + p * py, trk))
    return _path_obs(
        hex_, start, lat, lon, samples, dt_s, speed_kt, type_code=type_code, mil=mil, **kw
    )


def circle_orbit(
    hex_: str = "ae0002",
    start: datetime = T0,
    lat: float = 20.0,
    lon: float = 96.0,
    radius_km: float = 25.0,
    speed_kt: float = 300.0,
    minutes: float = 75.0,
    dt_s: float = 10.0,
    type_code: str = "E3TF",
    mil: bool = True,
    **kw: Any,
) -> list[AircraftObs]:
    v = speed_kt * KM_PER_NM / 3600.0
    samples = []
    for i in range(int(minutes * 60 / dt_s)):
        th = v * i * dt_s / radius_km  # clockwise from north
        x, y = radius_km * math.sin(th), radius_km * math.cos(th)
        samples.append((x, y, math.degrees(th) + 90.0))
    return _path_obs(
        hex_, start, lat, lon, samples, dt_s, speed_kt, type_code=type_code, mil=mil, **kw
    )


def heading_wrap_track(
    hex_: str = "ae0003",
    start: datetime = T0,
    lat: float = 20.0,
    lon: float = 96.0,
    minutes: float = 75.0,
    dt_s: float = 10.0,
    type_code: str = "K35R",
    mil: bool = True,
    **kw: Any,
) -> list[AircraftObs]:
    """Due-north military flight whose reported track alternates 359/1 (wrap-around trap)."""
    obs = straight_flight(
        hex_, start, lat, lon, 0.0, 420.0, minutes, dt_s, type_code=type_code, mil=mil, **kw
    )
    return [o.model_copy(update={"track": 359.0 if i % 2 else 1.0}) for i, o in enumerate(obs)]


def jammed_cell(
    start: datetime = T0,
    lat: float = 20.0,
    lon: float = 96.0,
    n_aircraft: int = 12,
    n_jammed: int = 4,
    minutes: float = 30.0,
    dt_s: float = 10.0,
    providers: Sequence[str] = PROVIDERS[:2],
    src_type: str = "adsb_icao",
    seed: int = 7,
    hex_prefix: str = "b",
    jam_kw: dict[str, Any] | None = None,
) -> list[AircraftObs]:
    """N aircraft crossing an H3 r4 cell centre within +/-10 km; the first `n_jammed` report
    NIC=0/NACp=0 (degraded GNSS). Each aircraft is reported by every provider in `providers`."""
    rng = random.Random(seed)
    clat, clon = cell_center(lat, lon)
    jam = {"nic": 0, "nac_p": 0} if jam_kw is None else jam_kw
    out: list[AircraftObs] = []
    for k in range(n_aircraft):
        hex_ = f"{hex_prefix}{k:05x}"
        hdg = rng.uniform(0, 360)
        t_start = start + timedelta(seconds=rng.uniform(0, max(0.0, minutes * 60 - 120)))
        la0, lo0 = destination(clat, clon, hdg + 180, 10.0)
        n = int(20 * 1.852 * 3600 / 450 / 1.852 / dt_s)  # ~20 km at 450 kt
        for i in range(max(3, n)):
            la, lo = destination(la0, lo0, hdg, 450 * KM_PER_NM * i * dt_s / 3600)
            kw: dict[str, Any] = {"track": hdg, "src_type": src_type}
            if src_type == "mlat":
                kw.update(nic=None, nac_p=None)
            elif k < n_jammed:
                kw.update(jam)
            for p in providers:
                out.append(
                    make_obs(hex_, t_start + timedelta(seconds=i * dt_s), la, lo, provider=p, **kw)
                )
    return out


def single_faulty_transponder(
    start: datetime = T0, lat: float = 20.0, lon: float = 96.0, n_aircraft: int = 12
) -> list[AircraftObs]:
    """Busy cell where exactly one airframe permanently reports NIC=0 (bad avionics)."""
    return jammed_cell(start, lat, lon, n_aircraft=n_aircraft, n_jammed=1)


def mlat_only_traffic(
    start: datetime = T0, lat: float = 20.0, lon: float = 96.0, n_aircraft: int = 12
) -> list[AircraftObs]:
    """Cell covered only by MLAT (no NIC/NACp) — cannot judge GNSS quality."""
    return jammed_cell(start, lat, lon, n_aircraft=n_aircraft, n_jammed=0, src_type="mlat")


def teleport_spoof(
    hex_: str = "c00001",
    start: datetime = T0,
    lat: float = 20.0,
    lon: float = 96.0,
    heading: float = 90.0,
    jump_after_min: float = 10.0,
    jump_km: float = 150.0,
    jump_bearing: float = 0.0,
    minutes: float = 20.0,
    dt_s: float = 10.0,
    nic: int = 8,
    **kw: Any,
) -> list[AircraftObs]:
    """Straight flight whose reported position jumps `jump_km` and then continues from there
    (GNSS spoofing capture), keeping a healthy NIC (receiver trusts the fake fix)."""
    obs = straight_flight(hex_, start, lat, lon, heading, 450.0, minutes, dt_s, nic=nic, **kw)
    jump_t = start + timedelta(minutes=jump_after_min)
    out = []
    for o in obs:
        if o.ts >= jump_t:
            la, lo = destination(o.lat, o.lon, jump_bearing, jump_km)
            o = make_obs(
                o.hex,
                o.ts,
                la,
                lo,
                **o.model_dump(exclude={"ts", "hex", "lat", "lon", "h3_r4", "h3_r6"}),
            )
        out.append(o)
    return out


def single_glitch(hex_: str = "c00002", **kw: Any) -> list[AircraftObs]:
    """Straight flight with one outlier fix 80 km off-track (single-report glitch)."""
    obs = straight_flight(hex_, minutes=20.0, **kw)
    i = len(obs) // 2
    o = obs[i]
    la, lo = destination(o.lat, o.lon, 0.0, 80.0)
    obs[i] = make_obs(
        o.hex, o.ts, la, lo, **o.model_dump(exclude={"ts", "hex", "lat", "lon", "h3_r4", "h3_r6"})
    )
    return obs


def alt_divergence(hex_: str = "c00003", offset_ft: int = 2500, **kw: Any) -> list[AircraftObs]:
    """Straight flight whose GNSS altitude diverges from baro altitude by `offset_ft`."""
    obs = straight_flight(hex_, minutes=20.0, **kw)
    return [o.model_copy(update={"alt_geom": (o.alt_baro or 0) + offset_ft}) for o in obs]


def mlat_vs_adsb(hex_: str = "c00004", offset_km: float = 20.0, **kw: Any) -> list[AircraftObs]:
    """Aircraft whose ADS-B (self-reported) positions sit `offset_km` away from where a second
    aggregator's MLAT places it (spoofed GNSS vs. ground truth multilateration)."""
    truth = straight_flight(hex_, minutes=20.0, dt_s=10.0, **kw)
    out = []
    for i, o in enumerate(truth):
        if i % 3 == 0:
            out.append(
                o.model_copy(
                    update={
                        "src_type": "mlat",
                        "provider": PROVIDERS[1],
                        "nic": None,
                        "nac_p": None,
                    }
                )
            )
        la, lo = destination(o.lat, o.lon, 0.0, offset_km)
        out.append(
            make_obs(
                o.hex,
                o.ts + timedelta(seconds=1),
                la,
                lo,
                **o.model_dump(exclude={"ts", "hex", "lat", "lon", "h3_r4", "h3_r6"}),
            )
        )
    return out


def spoof_cluster(
    start: datetime = T0,
    lat: float = 20.0,
    lon: float = 96.0,
    n: int = 4,
    minutes: float = 15.0,
    dt_s: float = 10.0,
) -> list[AircraftObs]:
    """`n` aircraft on different real headings all reporting (nearly) the same fixed position —
    the classic 'circle/point' spoof. Their gs/track stay truthful, so v_calc << gs."""
    out = []
    for k in range(n):
        hdg = (k * 360.0 / n + 10) % 360
        for i in range(int(minutes * 60 / dt_s)):
            la, lo = destination(lat, lon, (i * 37) % 360, 0.2)  # jitter < 1 km
            out.append(
                make_obs(
                    f"d{k:05x}", start + timedelta(seconds=i * dt_s), la, lo, track=hdg, gs=440.0
                )
            )
    return out
