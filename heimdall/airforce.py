"""Air force watch (task 056): one air force's visible fleet as a whole. Pure: no I/O.

From each aircraft's positions (days, from the lake) it derives flights (split at ground points and
gaps), where each flight began and ended (nearest airfield, OurAirports), and where each aircraft
is now: home base, known overseas training detachment, elsewhere in the home country, abroad, or
airborne. Movements between places and a daily flight count follow. Notes are rule-based and
factual: a stay at a known detachment is "routine"; a tanker / transport / early-warning aircraft
abroad elsewhere is a "possible deployment, exercise or transport mission"; flying well above the
fleet's own daily median is "above usual". Nothing here predicts intent.

Coverage: only airframes that broadcast ADS-B and are listed in plane-alert-db (fighters usually
are not).
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from heimdall.milactivity import GAP_S, LOW_FT, Point

Nearest = Callable[[float, float, float, bool], tuple[Any, float] | None]
FIELD_KM = 25.0
AIRBORNE_S = 20 * 60


def _ground(p: Point) -> bool:
    return p[3] < 0 and (p[4] < 0 or p[4] < 50)


def _low(p: Point) -> bool:
    return _ground(p) or 0 <= p[3] < LOW_FT


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


@dataclass
class Place:
    ident: str | None
    name: str
    country: str | None
    kind: str  # home | detachment | home_country | abroad | unknown
    unit: str | None = None


@dataclass
class Flight:
    hex: str
    dep_t: str
    arr_t: str
    dep: Place | None
    arr: Place | None
    hours: float


@dataclass
class AircraftState:
    hex: str
    ident: str
    type: str | None
    type_name: str | None
    status: str  # airborne | home | detachment | home_country | abroad | unknown
    place: Place | None
    since: str | None
    last_seen: str
    lat: float
    lon: float
    flights: int


@dataclass
class Note:
    level: str  # info | watch
    text: str
    hexes: list[str] = field(default_factory=list)


def classify_place(f: Any, cfg: Mapping[str, Any]) -> Place:
    ident = str(getattr(f, "ident", "") or "").upper()
    homes = {k.upper(): v for k, v in (cfg.get("home_bases") or {}).items()}
    dets = {k.upper(): v for k, v in (cfg.get("detachments") or {}).items()}
    country = getattr(f, "country", None)
    if ident in homes:
        return Place(ident, str(homes[ident]), country, "home")
    if ident in dets:
        d = dets[ident]
        return Place(ident, str(d.get("name", f.name)), country, "detachment", d.get("unit"))
    kind = "home_country" if country and country == cfg.get("home_country") else "abroad"
    return Place(ident or None, str(f.name), country, kind)


def _place_at(p: Point, nearest: Nearest, cfg: Mapping[str, Any], rotary: bool) -> Place | None:
    hit = nearest(p[1], p[2], FIELD_KM, rotary)
    return classify_place(hit[0], cfg) if hit else None


def flights_of(
    hex_: str, pts: Sequence[Point], nearest: Nearest, cfg: Mapping[str, Any], rotary: bool = False
) -> list[Flight]:
    """Flights = airborne runs between ground points / data gaps. Ends get the nearest airfield
    when the aircraft was low there; otherwise the end is open (left or entered coverage)."""
    pts = sorted(pts)
    out: list[Flight] = []
    seg: list[Point] = []

    def close(seg: list[Point], landed: Point | None) -> None:
        air = [p for p in seg if not _ground(p)]
        if not air:
            return
        first = seg[0]
        last = landed or seg[-1]
        dep = _place_at(first, nearest, cfg, rotary) if _low(first) else None
        arr = _place_at(last, nearest, cfg, rotary) if _low(last) else None
        out.append(
            Flight(
                hex_, _iso(first[0]), _iso(last[0]), dep, arr, round((last[0] - first[0]) / 3600, 2)
            )
        )

    prev: Point | None = None
    for p in pts:
        if prev is not None and p[0] - prev[0] > GAP_S:
            close(seg, None)
            seg = []
        if _ground(p):
            if seg and any(not _ground(x) for x in seg):
                close(seg, p)
            seg = [p]  # the ground point starts the next flight
        else:
            seg.append(p)
        prev = p
    if seg and any(not _ground(x) for x in seg):
        close(seg, None)
    return out


def state_of(
    hex_: str,
    ident: Mapping[str, Any],
    pts: Sequence[Point],
    flights: Sequence[Flight],
    nearest: Nearest,
    cfg: Mapping[str, Any],
    now: float,
    rotary: bool = False,
) -> AircraftState:
    last = max(pts)
    label = ident.get("flight") or ident.get("registration") or hex_.upper()
    base = dict(
        hex=hex_,
        ident=str(label),
        type=ident.get("type"),
        type_name=ident.get("type_name"),
        last_seen=_iso(last[0]),
        lat=last[1],
        lon=last[2],
        flights=len(flights),
    )
    if not _ground(last) and now - last[0] < AIRBORNE_S:
        return AircraftState(status="airborne", place=None, since=None, **base)  # type: ignore[arg-type]
    place = flights[-1].arr if flights and flights[-1].arr else None
    since = flights[-1].arr_t if place else None
    if place is None and _low(last):
        place = _place_at(last, nearest, cfg, rotary)
        since = _iso(last[0]) if place else None
    status = place.kind if place else "unknown"
    return AircraftState(status=status, place=place, since=since, **base)  # type: ignore[arg-type]


def analyse(
    fleet: Mapping[str, Mapping[str, Any]],
    tracks: Mapping[str, Sequence[Point]],
    nearest: Nearest,
    cfg: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    nowt = now.timestamp()
    states: list[AircraftState] = []
    all_flights: list[Flight] = []
    for h, pts in tracks.items():
        if not pts:
            continue
        ident = fleet.get(h, {})
        rotary = str(ident.get("role")) == "rotary"
        fl = flights_of(h, pts, nearest, cfg, rotary)
        all_flights += fl
        states.append(state_of(h, ident, pts, fl, nearest, cfg, nowt, rotary))
    states.sort(key=lambda s: (s.status != "airborne", s.status, s.type or "", s.ident))
    # a movement changes country (home -> abroad, between countries); local sorties at a
    # detachment (e.g. Pearce <-> Gingin) are not movements
    moves = [
        f
        for f in all_flights
        if f.dep and f.arr and f.dep.country and f.arr.country and f.dep.country != f.arr.country
    ]
    moves.sort(key=lambda f: f.arr_t, reverse=True)
    per_day: Counter[str] = Counter(f.dep_t[:10] for f in all_flights)
    days = sorted(per_day)
    notes = _notes(states, moves, per_day, cfg, fleet, now)
    return {
        "generated": now.isoformat(),
        "fleet_listed": len(fleet),
        "seen": len(states),
        "status": dict(Counter(s.status for s in states)),
        "types": dict(Counter(s.type or "?" for s in states).most_common()),
        "aircraft": [asdict(s) for s in states],
        "movements": [asdict(f) for f in moves[:40]],
        "flights_per_day": [{"day": d, "flights": per_day[d]} for d in days],
        "notes": [asdict(n) for n in notes],
        "limits": str(cfg.get("limits") or ""),
    }


def _notes(
    states: Sequence[AircraftState],
    moves: Sequence[Flight],
    per_day: Mapping[str, int],
    cfg: Mapping[str, Any],
    fleet: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> list[Note]:
    out: list[Note] = []
    watch = {str(t).upper() for t in cfg.get("watch_types") or []}
    by_det: dict[str, list[AircraftState]] = defaultdict(list)
    for s in states:
        if s.status == "detachment" and s.place:
            by_det[s.place.name].append(s)
    for name, ss in sorted(by_det.items()):
        unit = ss[0].place.unit if ss[0].place else None
        types = ", ".join(sorted({s.type or "?" for s in ss}))
        out.append(
            Note(
                "info",
                f"{len(ss)} aircraft ({types}) at {name}"
                + (f" — {unit}" if unit else "")
                + ": routine overseas training detachment.",
                [s.hex for s in ss],
            )
        )
    for s in states:
        if s.status != "abroad" or not s.place:
            continue
        since = f" since {s.since[:16].replace('T', ' ')} UTC" if s.since else ""
        strategic = (s.type or "").upper() in watch
        out.append(
            Note(
                "watch" if strategic else "info",
                f"{s.ident} ({s.type_name or s.type or 'unknown type'}) at {s.place.name}, "
                f"{s.place.country}{since}: away from home bases and known detachments"
                + (" — possible deployment, exercise or transport mission." if strategic else "."),
                [s.hex],
            )
        )
    recent = now.timestamp() - 72 * 3600
    for f in moves:
        if datetime.fromisoformat(f.arr_t).timestamp() < recent:
            continue
        t = str(fleet.get(f.hex, {}).get("type") or "").upper()
        if t in watch and f.dep and f.arr:
            out.append(
                Note(
                    "watch",
                    f"{fleet.get(f.hex, {}).get('registration') or f.hex.upper()} ({t}) flew "
                    f"{f.dep.name} ({f.dep.country}) → {f.arr.name} ({f.arr.country}), landed "
                    f"{f.arr_t[:16].replace('T', ' ')} UTC.",
                    [f.hex],
                )
            )
    days = sorted(per_day)
    if len(days) >= 4:
        today, prev = per_day[days[-1]], [per_day[d] for d in days[:-1]]
        med = statistics.median(prev)
        if today >= max(2 * med, med + 3):
            out.append(
                Note(
                    "watch",
                    f"Flying activity above usual: {today} flights on {days[-1]} vs a daily "
                    f"median of {med:g} over the previous {len(prev)} days.",
                )
            )
    return out
