# ruff: noqa: E501  (demo script: long human-readable strings)
"""Operation Midnight Hammer demo (task 040): real ADS-B, 18-22 June 2025, through Heimdall.

1. Merges the per-day military parquets from `fetch_adsblol_day.sh D --mil --every 60` (adsb.lol,
   ODbL) into data/replay/midnight_hammer/midnight_hammer.parquet.
2. Replays them hour by hour through the real military pipeline (`military_step`: tracks, theatre
   detectors, fusion) and writes the alerts/evidence bundle next to the parquet.
3. Checks each indicator of the pre-strike brief (military-aircraft-tracker.com intel brief, plus
   Flightradar24 / The Aviationist reporting) against the recorded data and writes a short
   scenario (`midnight_hammer.scenario.json`) that the demo app plays step by step. Every step
   says whether the indicator is SEEN in the data, NOT VISIBLE to ADS-B, or has NO DATA in Heimdall.

Usage: uv run python scripts/midnight_hammer.py   (then: uv run python -m heimdall.api.demo
       --replay data/replay/midnight_hammer/midnight_hammer.parquet --port 8030)
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

os.environ["HEIMDALL_MIL_STATE"] = ""  # never touch the live military state file

from heimdall import config
from heimdall.backtest.replay import ReplayStore
from heimdall.backtest.replay_io import (
    bundle_paths,
    read_obs_parquet,
    write_obs_parquet,
)
from heimdall.fuse.admiralty import grades_from_sources
from heimdall.geo import haversine_km
from heimdall.ingest.plane_alert import index
from heimdall.military_step import MIL_KINDS, military_step, theatres
from heimdall.milroles import MilIdent, classify
from heimdall.miltrack import MilTracks
from heimdall.pipeline import _assign_tick_persistence, fuse_and_store
from heimdall.schemas import AircraftObs, Alert, AlertKind

DIR = Path("data/replay/midnight_hammer")
OUT = DIR / "midnight_hammer.parquet"
T0 = datetime(2025, 6, 16, 12, tzinfo=UTC)  # archives of 18 Jun reach back to 16 Jun
T1 = datetime(2025, 6, 23, tzinfo=UTC)
STRIKE = datetime(2025, 6, 21, 23, 0, tzinfo=UTC)  # ~02:30 Iran time 22 Jun = 23:00 UTC 21 Jun


def load() -> list[AircraftObs]:
    obs: list[AircraftObs] = []
    for p in sorted(DIR.glob("2025-06-*.parquet")):
        day = read_obs_parquet(p)
        print(f"{p.name}: {len(day):,} rows, {len({o.hex for o in day})} aircraft")
        obs += day
    if not obs:
        sys.exit("no input: run scripts/fetch_adsblol_day.sh D --mil --every 60 for 18-22 Jun")
    write_obs_parquet(OUT, obs)
    return obs


def run_pipeline(obs: Sequence[AircraftObs]) -> tuple[list[Alert], list[Any]]:
    s = config.settings()
    mcfg = {**s.detectors["military"], "step_min": 60}  # hourly ticks over the recording
    fcfg = dict(s.detectors.get("fusion", {}))
    grades = grades_from_sources(config.sources())
    store = ReplayStore()
    store.insert_aircraft(list(obs))
    evidence: dict[str, Any] = {}
    now = T0 + timedelta(hours=1)
    while now <= T1:
        for e in military_step(
            store,
            mcfg,
            [],
            [],
            now,
            lambda name, evs, now=now: fuse_and_store(
                store,
                name,
                evs,
                fcfg,
                config.playbook(),
                grades,
                now,
                own=lambda k: k in MIL_KINDS,
            ),
            _assign_tick_persistence,
        ):
            evidence[e.id] = e
        now += timedelta(hours=1)
    alerts = sorted(store.list_alerts(limit=100_000), key=lambda a: a.first_seen)
    return alerts, sorted(evidence.values(), key=lambda e: (e.window_end, e.id))


# ------------------------------------------------------------------ indicator checks


def tracks_of(obs: Iterable[AircraftObs]) -> tuple[MilTracks, dict[str, MilIdent]]:
    t = MilTracks(step_s=60, keep_h=10_000)
    t.add(obs)
    db = index()
    return t, {h: classify(h, tr.type_code, db.get(h)) for h, tr in t.tracks.items()}


def label(h: str, t: MilTracks, ids: dict[str, MilIdent]) -> str:
    tr, i = t.tracks[h], ids[h]
    return f"{tr.flight or tr.registration or h.upper()} ({i.type_name or tr.type_code or '?'})"


def in_window(
    t: MilTracks,
    hexes: Iterable[str],
    a: datetime,
    b: datetime,
    where: tuple[float, float, float] | None = None,
    airborne: bool = True,
) -> dict[str, list[tuple[float, float, float, float, float]]]:
    """hex -> its points in [a, b) (optionally within (lat, lon, km), airborne only)."""
    out = {}
    for h in hexes:
        pts = [
            p
            for p in t.tracks[h].points
            if a.timestamp() <= p[0] < b.timestamp()
            and (not airborne or p[3] >= 0)
            and (where is None or haversine_km(p[1], p[2], where[0], where[1]) <= where[2])
        ]
        if pts:
            out[h] = pts
    return out


def first_alert(
    alerts: Sequence[Alert], kinds: Sequence[AlertKind], aoi: str | None
) -> Alert | None:
    return next((a for a in alerts if a.kind in kinds and (aoi is None or a.aoi == aoi)), None)


def iso(ts: float | datetime) -> str:
    d = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def crossings(
    t: MilTracks, ids: dict[str, MilIdent], roles: Sequence[str], a: datetime, b: datetime
) -> list[tuple[float, str, tuple[float, float, float, float, float]]]:
    """Eastbound Atlantic crossings: seen west of 35 W, later east of 15 W (north of 30 N).
    Returns (time first seen east, hex, last point) for crossings that reach the east in [a, b)."""
    out = []
    for h, tr in t.tracks.items():
        if ids[h].role not in roles:
            continue
        west = next((p for p in tr.points if p[2] < -35), None)
        if west is None:
            continue
        east = next((p for p in tr.points if p[0] > west[0] and p[2] > -15 and p[1] > 30), None)
        if east is not None and a.timestamp() <= east[0] < b.timestamp():
            # where it went in Europe / the Middle East: last point east of 15 W before b
            dest = [p for p in tr.points if p[0] >= east[0] and p[0] < b.timestamp() and p[2] > -15]
            out.append((east[0], h, dest[-1] if dest else east))
    return sorted(out)


def busiest(
    t: MilTracks,
    hexes: Iterable[str],
    a: datetime,
    b: datetime,
    box: tuple[float, float, float, float] = (30.0, -45.0, 62.0, -8.0),
) -> datetime:
    """Hour in [a, b) with the most of `hexes` airborne inside box (S, W, N, E): the map moment."""
    best, best_n = a, -1
    hs = list(hexes)
    h = a
    while h < b:
        lo, hi = h.timestamp() - 1800, h.timestamp() + 1800
        n = sum(
            1
            for x in hs
            if any(
                lo <= p[0] < hi
                and p[3] >= 0
                and box[0] <= p[1] <= box[2]
                and box[1] <= p[2] <= box[3]
                for p in t.tracks[x].points
            )
        )
        if n > best_n:
            best, best_n = h, n
        h += timedelta(hours=1)
    return best


def near_base(p: tuple[float, ...]) -> str:
    for name, la, lo in BASES:
        if haversine_km(p[1], p[2], la, lo) <= 60:
            return name
    return f"{p[1]:.1f}N {p[2]:.1f}E"


#: approximate positions, only to name where an aircraft ended up (60 km match)
BASES = (
    ("Ramstein", 49.44, 7.60),
    ("Spangdahlem", 49.97, 6.69),
    ("Mildenhall", 52.36, 0.49),
    ("Lakenheath", 52.41, 0.56),
    ("Fairford", 51.68, -1.79),
    ("Moron", 37.17, -5.62),
    ("Rota", 36.64, -6.35),
    ("Lajes", 38.76, -27.09),
    ("Souda Bay", 35.53, 24.15),
    ("Akrotiri", 34.59, 32.99),
    ("Aviano", 46.03, 12.60),
    ("Sigonella", 37.40, 14.92),
    ("Incirlik", 37.00, 35.43),
    ("Al Udeid", 25.12, 51.31),
    ("Mihail Kogalniceanu", 44.36, 28.49),
    ("Prestwick", 55.51, -4.59),
    ("Pratica di Mare", 41.65, 12.45),
    ("Andrews", 38.81, -76.87),
    ("Tinker", 35.41, -97.39),
    ("Offutt", 41.12, -95.91),
)


def scenario(obs: Sequence[AircraftObs], alerts: Sequence[Alert]) -> dict[str, Any]:
    t, ids = tracks_of(obs)
    th = {x.name: x for x in theatres(config.settings().detectors["military"], [])}
    gulf = th["Gulf"]
    by_role: dict[str, list[str]] = {}
    for h, i in ids.items():
        by_role.setdefault(i.role, []).append(h)
    steps: list[dict[str, Any]] = []

    def step(**kw: Any) -> None:
        kw["zoom"] = max(3.8, kw["zoom"])  # below ~3.3 the map may stay in globe mode
        steps.append(kw)
        print(f"- {kw['when']}: {kw['title']} [{kw['status']}] {kw['saw']}")

    def alert_for(
        kinds: Sequence[AlertKind],
        aoi: str | None = None,
        role: str | None = None,
        at: datetime | None = None,
    ) -> str | None:
        """The matching alert active at `at` (else the first matching one)."""
        ok = [
            a
            for a in alerts
            if a.kind in kinds
            and (aoi is None or a.aoi == aoi)
            and (role is None or role in a.summary)
        ]
        if at is not None:
            slack = timedelta(hours=2)  # hourly ticks: an alert opens up to an hour after
            near = [a for a in ok if a.first_seen - slack <= at <= a.last_seen + slack]
            if near:
                return min(near, key=lambda a: abs(a.first_seen - at)).id
        return ok[0].id if ok else None

    def ends(rows: Sequence[tuple[float, str, Any]]) -> str:
        return ", ".join(
            f"{n} {k}" for k, n in Counter(near_base(r[2]) for r in rows).most_common(4)
        )

    # 1 airlift
    lift = crossings(t, ids, ["airlift"], T0 - timedelta(days=2), STRIKE)
    lift_gulf = in_window(
        t, by_role.get("airlift", []), T0, STRIKE, (gulf.lat, gulf.lon, gulf.radius_km)
    )
    types = Counter(t.tracks[h].type_code for _, h, _ in lift)
    step(
        when="16-19 Jun",
        title="Airlift wave across the Atlantic",
        brief="8+ C-17 to Al Udeid; 3 C-5M to Diego Garcia (strongest single indicator)",
        saw=f"{len(lift)} transport aircraft crossed the Atlantic eastbound before the strike "
        f"({', '.join(f'{n} {k}' for k, n in types.most_common(3))}); last seen at {ends(lift)}. "
        f"Over the Gulf only {len(lift_gulf)} transports broadcast; Diego Garcia has no receivers",
        status="seen" if lift else "not_visible",
        at=iso(busiest(t, [h for _, h, _ in lift], T0, T0 + timedelta(days=3)))
        if lift
        else iso(T0),
        lat=48.0,
        lon=-6.0,
        zoom=3.8,
        examples=[label(h, t, ids) for _, h, _ in lift[:6]],
        alert=alert_for([AlertKind.MIL_DEPLOYMENT], role="transport"),
    )
    # 2 bombers
    b2 = [h for h in by_role.get("bomber", []) if (t.tracks[h].type_code or "").upper() == "B2"]
    step(
        when="18-21 Jun",
        title="B-2 bombers",
        brief="6 B-2A to Diego Garcia, transponders off offshore",
        saw="no B-2 broadcast ADS-B on any day: the strike bombers flew dark, so Heimdall cannot see "
        "them. Their support chain (tankers, airlift) is what shows",
        status="seen" if b2 else "not_visible",
        at=iso(T0 + timedelta(days=1)),
        lat=38.7,
        lon=-93.5,
        zoom=4,
        examples=[],
        alert=None,
    )
    # 3 tankers
    tank = crossings(t, ids, ["tanker"], T0 - timedelta(days=2), STRIKE)
    tank_gulf = in_window(
        t, by_role.get("tanker", []), T0, STRIKE, (gulf.lat, gulf.lon, gulf.radius_km)
    )
    step(
        when="16-21 Jun",
        title="Tanker deployment to European bases",
        brief="15+ KC-135R to Al Udeid, Prince Sultan, Al Dhafra; reporting: ~30 tankers to Europe, Lajes, Souda",
        saw=f"{len(tank)} tankers crossed the Atlantic eastbound; last seen at {ends(tank)}. "
        f"Tankers broadcasting over the Gulf: {len(tank_gulf)}",
        status="seen" if tank else "not_visible",
        at=iso(busiest(t, [h for _, h, _ in tank], T0, T0 + timedelta(days=3)))
        if tank
        else iso(T0),
        lat=45.0,
        lon=-4.0,
        zoom=3.8,
        examples=[label(h, t, ids) for _, h, _ in tank[:6]],
        alert=alert_for([AlertKind.MIL_DEPLOYMENT, AlertKind.MIL_SURGE], role="tanker"),
    )
    # 4 ISR
    strategic_isr = [
        h
        for h in by_role.get("isr", [])
        if (t.tracks[h].type_code or "") in ("R135", "Q4", "U2", "E3TF", "E3CF", "E8")
    ]
    isr = in_window(t, strategic_isr, T0, STRIKE)
    # Atlantic to Iran: where strike support would show
    abroad = {h: p for h, p in isr.items() if any(-30 < q[2] < 65 and 10 < q[1] < 62 for q in p)}
    isr_gulf = {
        h: p
        for h, p in isr.items()
        if any(haversine_km(q[1], q[2], gulf.lat, gulf.lon) <= gulf.radius_km for q in p)
    }
    us_east = {
        h: p
        for h, p in abroad.items()
        if ids[h].country == "United States" and any(q[2] > 20 for q in p)
    }
    first_isr = min((p[0][0] for p in (us_east or abroad).values()), default=None)
    pos4 = next(iter((us_east or abroad).values()))[0] if (us_east or abroad) else (0, 37.0, 20.0)
    step(
        when="18-21 Jun",
        title="Surveillance aircraft",
        brief="RC-135W racetracks on the Iran-Iraq border, RQ-4 FORTE over the Gulf of Oman, U-2, E-3G",
        saw=(
            "US strategic surveillance east of Italy: "
            + ", ".join(f"{label(h, t, ids)} near {near_base(p[0])}" for h, p in us_east.items())
            + f". Over the Gulf: {len(isr_gulf)}; theatre surveillance flies dark"
        )
        if us_east
        else f"no US strategic surveillance broadcast east of Italy; only {len(abroad)} allied E-3/RQ-4 "
        f"over Europe (routine). Over the Gulf: {len(isr_gulf)}; theatre surveillance flies dark",
        status="seen" if us_east else "not_visible",
        at=iso(first_isr) if first_isr else iso(T0),
        lat=pos4[1],
        lon=pos4[2],
        zoom=5,
        examples=[label(h, t, ids) for h in list(us_east or abroad)[:6]],
        alert=alert_for([AlertKind.MIL_BUILDUP], "East Mediterranean")
        or alert_for([AlertKind.MIL_BUILDUP]),
    )
    # 5 command posts
    c2 = in_window(
        t, by_role.get("c2", []), STRIKE - timedelta(hours=3), STRIKE + timedelta(hours=3)
    )
    c2_all = in_window(t, by_role.get("c2", []), T0, T1)
    first = min((p[0][0] for p in c2.values()), default=None)
    pos = next(iter(c2.values()))[0] if c2 else (0, 35.4, -97.4)
    step(
        when="21 Jun",
        title="Airborne command posts at strike time",
        brief="E-6B over the Indian Ocean; E-4B airborne from Andrews",
        saw=(
            f"{', '.join(label(h, t, ids) for h in c2)} airborne within 3 h of the strike, near "
            f"{near_base(pos)} (not over the Indian Ocean). "
            f"{len(c2_all)} E-4B / E-6B flew 18-22 Jun"
        )
        if c2
        else f"{len(c2_all)} E-4B / E-6B flights 18-22 Jun, none around strike time",
        status="seen" if c2 else ("seen" if c2_all else "not_visible"),
        at=iso(first) if first else iso(STRIKE),
        lat=pos[1],
        lon=pos[2],
        zoom=5,
        examples=[label(h, t, ids) for h in list(c2_all)[:6]],
        alert=alert_for(
            [AlertKind.MIL_HIGH_INTEREST],
            "Continental US",
            at=datetime.fromtimestamp(first, UTC) if first else STRIKE,
        ),
    )
    # 6 notices
    step(
        when="21 Jun",
        title="Airspace closures (NOTAMs)",
        brief="NOTAMs close the Arabian Sea, Gulf of Oman and southern Iran corridors",
        saw="no 2025 NOTAM archive in Heimdall. In live runs FAA NOTAMs and NGA warnings count as a "
        "build-up indicator",
        status="no_data",
        at=iso(STRIKE - timedelta(hours=6)),
        lat=24.0,
        lon=60.0,
        zoom=4.5,
        examples=[],
        alert=None,
    )
    # 7 strike night
    after = in_window(t, by_role.get("tanker", []), STRIKE, T1, (36.0, 20.0, 1500))
    step(
        when="22 Jun",
        title="Strike night: tankers over the Mediterranean",
        brief="Strikes 02:30 Iran time (23:00 UTC 21 Jun); reported: KC-135s west of Crete, tankers NE of the Azores",
        saw=f"after 23:00 UTC: {len(after)} tankers airborne over the central and eastern "
        f"Mediterranean ({', '.join(label(h, t, ids) for h in list(after)[:3])})",
        status="seen" if after else "not_visible",
        at=iso(min((p[0][0] for p in after.values()), default=STRIKE.timestamp() + 3600)),
        lat=35.5,
        lon=22.0,
        zoom=4.5,
        examples=[label(h, t, ids) for h in list(after)[:6]],
        alert=alert_for([AlertKind.MIL_BUILDUP]),
    )
    seen = sum(1 for s in steps if s["status"] == "seen")
    kinds = Counter(a.kind for a in alerts)
    return {
        "title": "Operation Midnight Hammer: pre-strike indicators (real ADS-B, 16-22 Jun 2025)",
        "summary": f"{seen} of {len(steps)} brief indicators visible in open ADS-B. Heimdall raised "
        + ", ".join(
            f"{n} {k.value.replace('mil_', '').replace('_', ' ')}" for k, n in kinds.most_common()
        )
        + " alerts from the same data.",
        "caveat": "Bombers and theatre surveillance flew dark, and the Gulf and Diego Garcia have no "
        "receivers: the visible signal is the logistics chain into Europe. Reporting says a visible "
        "westbound B-2 group was a decoy. Indicators show preparation, never a target or a strike.",
        "sources": "adsb.lol (ODbL), plane-alert-db (ODbL); brief: military-aircraft-tracker.com; "
        "Flightradar24, The Aviationist, ItaMilRadar",
        "steps": steps,
    }


STRATEGIC_ROLES = ("tanker", "airlift", "isr", "c2", "bomber")


def _bearing(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    import math

    la1, la2 = math.radians(a[1]), math.radians(b[1])
    dl = math.radians(b[2] - a[2])
    y = math.sin(dl) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def snapshot(t: MilTracks, ids: dict[str, MilIdent], at: datetime) -> list[dict[str, Any]]:
    """Military aircraft at `at` (last fix within 15 min) with a 3 h trail (5-min steps) for the
    strategic roles: what the Demo page draws for one step (task 043)."""
    ts = at.timestamp()
    out = []
    for h, tr in t.tracks.items():
        pts = [p for p in tr.points if ts - 10800 <= p[0] <= ts]
        if not pts or ts - pts[-1][0] > 900:
            continue
        last = pts[-1]
        i = ids[h]
        trail: list[list[float]] = []
        if i.role in STRATEGIC_ROLES:
            for p in pts:
                if not trail or p[0] - trail[-1][2] >= 300:
                    trail.append([round(p[2], 3), round(p[1], 3), p[0]])
        out.append(
            {
                "hex": h,
                "flight": tr.flight,
                "type": tr.type_code,
                "type_name": i.type_name,
                "role": i.role,
                "operator": i.operator,
                "country": i.country,
                "lat": round(last[1], 4),
                "lon": round(last[2], 4),
                "alt": None if last[3] < 0 else int(last[3]),
                "track": round(_bearing(pts[-2], last), 1) if len(pts) > 1 else None,
                "trail": [[x, y] for x, y, _ in trail],
            }
        )
    return out


def write_pack(
    sc: dict[str, Any], obs: Sequence[AircraftObs], alerts: Sequence[Alert], evidence: Sequence[Any]
) -> Path:
    """`<stem>.demo.json`: the scenario plus everything the live Demo page needs (no store)."""
    t, ids = tracks_of(obs)
    steps = [
        {**s, "snapshot": snapshot(t, ids, datetime.fromisoformat(s["at"].replace("Z", "+00:00")))}
        for s in sc["steps"]
    ]
    used = {s["alert"] for s in steps if s.get("alert")}
    keep_alerts = [a for a in alerts]  # all of them: the demo alert list shows the week's alerts
    ev_ids = {i for a in keep_alerts for i in a.evidence_ids}
    pack = {
        **{k: v for k, v in sc.items() if k != "steps"},
        "name": OUT.stem,
        "steps": steps,
        "alerts": [a.model_dump(mode="json") for a in keep_alerts],
        "evidence": [e.model_dump(mode="json") for e in evidence if e.id in ev_ids],
        "linked_alerts": sorted(used),
    }
    p = OUT.with_name(OUT.stem + ".demo.json")
    p.write_text(json.dumps(pack, separators=(",", ":")))
    print(f"demo pack {p} ({p.stat().st_size / 1e6:.1f} MB)")
    return p


def main() -> None:
    obs = load()
    alerts, evidence = run_pipeline(obs)
    a_p, e_p = bundle_paths(OUT)
    a_p.write_text("".join(a.model_dump_json() + "\n" for a in alerts))
    e_p.write_text("".join(e.model_dump_json() + "\n" for e in evidence))
    print(f"{len(alerts)} alerts, {len(evidence)} evidence")
    for a in alerts[:40]:
        print(
            f"  {iso(a.first_seen)} {a.kind.value:18s} {a.aoi:22s} {a.severity.value:7s} {a.summary[:110]}"
        )
    sc = scenario(obs, alerts)
    OUT.with_name(OUT.stem + ".scenario.json").write_text(json.dumps(sc, indent=1))
    write_pack(sc, obs, alerts, evidence)
    print(sc["summary"])


if __name__ == "__main__":
    main()
