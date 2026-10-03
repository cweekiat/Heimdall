"""Where a military aircraft came from and what it may be doing (task 047). Pure: no I/O.

Input is the aircraft's own track: points (epoch s, lat, lon, alt ft or -1 on ground / unknown,
gs kt or -1), from `MilTracks` (72 h, one per 5 min) merged with recent hot-store fixes. Each answer
carries the measured reasons, and words of estimative probability: "likely" when the geometry and
the role agree, "possible" otherwise. Intent is never known; nothing here names a target.

Take-off: the track is split into flights at ground points and at gaps > `GAP_S` (the aircraft was
not seen). The current flight "took off from" the airfield nearest its first point when that point
is on the ground or low (< 3,000 ft) near a field; else it was "first seen" airborne.
Activity rules (priors; thresholds below, from typical refuelling tracks and patrol patterns):
on ground · climbing out · descending to land · orbit / racetrack (>= 1.5 turns, net/path < 0.35) ·
transit (net/path >= 0.8, level) · low and slow · close to a tanker (<= 5 km, <= 1,500 ft).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from heimdall.geo import haversine_km

Point = tuple[float, float, float, float, float]  # t, lat, lon, alt_ft (-1 ground/unknown), gs
Nearest = Callable[[float, float, float, bool], tuple[Any, float] | None]  # lat, lon, km, heliports

GAP_S = 90 * 60
LOW_FT = 3000.0
WINDOW_S = 45 * 60
ORBIT_TURN_DEG = 540.0
ORBIT_NET_RATIO = 0.35
TRANSIT_NET_RATIO = 0.8
FORMATION_KM = 5.0
FORMATION_FT = 1500.0
RECEIVERS = ("fighter", "bomber", "isr", "c2", "airlift", "special", "uav", "other")
COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


def _ground(p: Point) -> bool:
    return p[3] < 0 and (p[4] < 0 or p[4] < 50)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass(b: float) -> str:
    return COMPASS[int(((b + 22.5) % 360) // 45)]


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def _field(f: Any, d: float) -> dict[str, Any]:
    return {
        "ident": f.ident,
        "name": f.name,
        "country": f.country,
        "military": f.military,
        "kind": f.kind,
        "lat": f.lat,
        "lon": f.lon,
        "distance_km": round(d, 1),
    }


def current_flight(points: Sequence[Point]) -> list[Point]:
    """Points of the current flight (after the last ground point or data gap), oldest first.
    The last ground point before take-off is kept as the first element."""
    pts = sorted(points)
    if not pts:
        return []
    start = 0
    for i in range(len(pts) - 1, 0, -1):
        if pts[i][0] - pts[i - 1][0] > GAP_S:
            start = i
            break
        if _ground(pts[i - 1]) and not _ground(pts[i]):
            start = i - 1
            break
    return pts[start:]


def departure(points: Sequence[Point], nearest: Nearest, rotary: bool = False) -> dict[str, Any]:
    """{kind: took_off | first_seen | on_ground | unknown, at, lat, lon, airfield, text}."""
    pts = sorted(points)
    if not pts:
        return {"kind": "unknown", "text": "No track yet."}
    if _ground(pts[-1]):
        hit = nearest(pts[-1][1], pts[-1][2], 5.0, rotary)
        where = f" at {hit[0].name}" if hit else ""
        return {
            "kind": "on_ground",
            "at": _iso(pts[-1][0]),
            "lat": pts[-1][1],
            "lon": pts[-1][2],
            "airfield": _field(*hit) if hit else None,
            "text": f"On the ground{where}.",
        }
    fl = current_flight(pts)
    p0 = fl[0]
    low = _ground(p0) or 0 <= p0[3] < LOW_FT
    hit = nearest(p0[1], p0[2], 15.0 if _ground(p0) else 25.0, rotary) if low else None
    when = datetime.fromtimestamp(p0[0], UTC).strftime("%d %b %H:%M UTC")
    if hit:
        f, d = hit
        verb = "Took off from" if _ground(p0) else "Climbing out of"
        return {
            "kind": "took_off",
            "at": _iso(p0[0]),
            "lat": p0[1],
            "lon": p0[2],
            "airfield": _field(f, d),
            "text": f"{verb} {f.name} ({f.ident}, {f.country}) around {when}"
            + ("" if _ground(p0) else f", first seen at {int(p0[3]):,} ft")
            + ".",
        }
    near = nearest(p0[1], p0[2], 150.0, False)
    alt = "on the ground" if p0[3] < 0 else f"at {int(p0[3]):,} ft"
    where = f", {near[1]:.0f} km from {near[0].name}" if near else ""
    return {
        "kind": "first_seen",
        "at": _iso(p0[0]),
        "lat": p0[1],
        "lon": p0[2],
        "airfield": _field(*near) if near else None,
        "text": f"First seen {when} {alt} at {p0[1]:.2f}, {p0[2]:.2f}{where}; "
        "take-off not covered by receivers.",
    }


@dataclass
class Shape:
    n: int
    path_km: float
    net_km: float
    turn_deg: float
    alt_first: float
    alt_last: float
    gs: float
    bearing: float

    @property
    def net_ratio(self) -> float:
        return self.net_km / self.path_km if self.path_km > 0 else 1.0


def shape(points: Sequence[Point]) -> Shape | None:
    pts = [p for p in sorted(points) if not _ground(p)]
    if len(pts) < 3:
        return None
    legs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    path = sum(haversine_km(a[1], a[2], b[1], b[2]) for a, b in legs)
    hdg = [
        bearing_deg(a[1], a[2], b[1], b[2])
        for a, b in legs
        if haversine_km(a[1], a[2], b[1], b[2]) > 0.2
    ]
    turn = sum(abs((hdg[i + 1] - hdg[i] + 540) % 360 - 180) for i in range(len(hdg) - 1))
    gss = [p[4] for p in pts if p[4] >= 0]
    alts = [p[3] for p in pts if p[3] >= 0]
    return Shape(
        n=len(pts),
        path_km=path,
        net_km=haversine_km(pts[0][1], pts[0][2], pts[-1][1], pts[-1][2]),
        turn_deg=turn,
        alt_first=alts[0] if alts else -1,
        alt_last=alts[-1] if alts else -1,
        gs=sum(gss) / len(gss) if gss else -1,
        bearing=bearing_deg(pts[0][1], pts[0][2], pts[-1][1], pts[-1][2]),
    )


@dataclass
class Activity:
    label: str
    confidence: str  # likely | possible
    reasons: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)


ORBIT_BY_ROLE = {
    "tanker": ("Likely air-refuelling track", "likely"),
    "isr": ("Likely surveillance orbit", "likely"),
    "c2": ("Likely airborne command / relay orbit", "likely"),
    "uav": ("Likely surveillance orbit", "likely"),
    "trainer": ("Circling: likely training or local flying", "likely"),
    "fighter": ("Circling: possible combat air patrol or training", "possible"),
    "rotary": ("Circling: possible search, patrol or training", "possible"),
}


def activity(
    points: Sequence[Point],
    role: str,
    now: float,
    nearest: Nearest,
    theatres: Sequence[Mapping[str, Any]] = (),
    others: Sequence[Mapping[str, Any]] = (),
    alerts: Sequence[Mapping[str, Any]] = (),
) -> Activity:
    """`others`: other military aircraft now {hex, flight, role, lat, lon, alt};
    `theatres`: {name, lat, lon, radius_km}; `alerts`: open alerts {ref, what, lat, lon}."""
    pts = sorted(points)
    if not pts:
        return Activity("No track yet", "possible")
    last = pts[-1]
    rotary = role == "rotary"
    ctx: list[str] = []
    for th in theatres:
        d = haversine_km(last[1], last[2], float(th["lat"]), float(th["lon"]))
        if d <= float(th["radius_km"]):
            ctx.append(f"Inside the {th['name']} theatre.")
    for a in alerts:
        d = haversine_km(last[1], last[2], float(a["lat"]), float(a["lon"]))
        if d <= 150:
            ctx.append(f"{d:.0f} km from open alert {a.get('ref') or ''} ({a.get('what')}).")
    if now - last[0] > 20 * 60:
        ctx.append(
            f"Last position {int((now - last[0]) // 60)} min ago: it may have left coverage."
        )
    if _ground(last):
        hit = nearest(last[1], last[2], 5.0, rotary)
        return Activity(
            f"On the ground{f' at {hit[0].name}' if hit else ''}",
            "likely",
            ["Reports on ground / no altitude."],
            ctx,
        )
    # close to a tanker (or, for a tanker, a receiver close by)
    if last[3] >= 0:
        for o in others:
            try:
                d = haversine_km(last[1], last[2], float(o["lat"]), float(o["lon"]))
                dz = abs(float(o.get("alt") or -1e9) - last[3])
            except (TypeError, ValueError):
                continue
            if d > FORMATION_KM or dz > FORMATION_FT:
                continue
            pair = {role, str(o.get("role"))}
            if "tanker" in pair and (pair - {"tanker"}) <= set(RECEIVERS) | {"tanker"}:
                who = o.get("flight") or str(o.get("hex", "")).upper()
                return Activity(
                    "Possibly air-to-air refuelling" if len(pair) > 1 else "Two tankers together",
                    "possible",
                    [f"{d:.1f} km and {dz:.0f} ft from {who} ({o.get('role')})."],
                    ctx,
                )
    recent = [p for p in pts if p[0] >= last[0] - WINDOW_S]
    s = shape(recent)
    if s is None:
        return Activity("Too few recent positions to judge", "possible", [], ctx)
    why = (
        f"Last {int((recent[-1][0] - recent[0][0]) // 60)} min: {s.path_km:.0f} km flown, "
        f"{s.net_km:.0f} km apart start to end, {s.turn_deg:.0f}° of turning, "
        f"{int(s.alt_first):,} → {int(s.alt_last):,} ft."
    )
    if s.alt_last >= 0 and s.alt_first >= 0:
        climb, drop = s.alt_last - s.alt_first, s.alt_first - s.alt_last
        if climb >= 3000 and s.alt_last < 20000:
            return Activity("Climbing out", "likely", [why], ctx)
        if drop >= 3000 and s.alt_last < 12000:
            hit = nearest(last[1], last[2], 60.0, rotary)
            at = f", possibly landing at {hit[0].name} ({hit[1]:.0f} km)" if hit else ""
            return Activity(f"Descending{at}", "likely" if hit else "possible", [why], ctx)
    if s.turn_deg >= ORBIT_TURN_DEG and s.net_ratio < ORBIT_NET_RATIO and s.path_km >= 20:
        label, conf = ORBIT_BY_ROLE.get(role, ("Orbiting / racetrack pattern", "possible"))
        return Activity(label, conf, [why], ctx)
    if 0 <= s.alt_last < LOW_FT and 0 <= s.gs < 180 and not nearest(last[1], last[2], 10.0, rotary):
        return Activity("Low and slow: possible patrol, search or training", "possible", [why], ctx)
    if s.net_ratio >= TRANSIT_NET_RATIO and abs(s.alt_last - s.alt_first) < 3000:
        label = f"Transit heading {compass(s.bearing)}"
        conf = "likely"
        for th in theatres:
            d = haversine_km(last[1], last[2], float(th["lat"]), float(th["lon"]))
            if d <= float(th["radius_km"]) or d > 4000:
                continue
            off = abs(
                (
                    bearing_deg(last[1], last[2], float(th["lat"]), float(th["lon"]))
                    - s.bearing
                    + 540
                )
                % 360
                - 180
            )
            if off <= 20:
                label += f" towards the {th['name']} theatre ({d:.0f} km)"
                if role in ("airlift", "tanker", "bomber"):
                    ctx.append("A strategic type heading into a theatre: possible deployment flow.")
                break
        return Activity(label, conf, [why], ctx)
    return Activity("Manoeuvring: pattern unclear", "possible", [why], ctx)
