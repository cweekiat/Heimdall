# ruff: noqa: RUF001
"""Pure parsing helpers for navigational warnings and NOTAMs (no I/O).

All input text is UNTRUSTED data. It is parsed with bounded regexes only; it is never evaluated,
followed or used as instructions, and nothing extracted from it is fetched.
"""

from __future__ import annotations

import calendar
import hashlib
import html as _html
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

import h3

from heimdall.geo import EARTH_RADIUS_KM, KM_PER_NM, h3_cell
from heimdall.schemas import Domain, GeoEvent

MAX_TEXT = 8000  # chars kept per notice
MAX_INPUT = 200_000  # chars ever scanned before capping (bounds work on hostile input)
MAX_COORDS = 500
CIRCLE_VERTICES = 32
CST = timezone(timedelta(hours=8))  # China Standard Time

_BLOCK_TAGS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "section"}
_CTRL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f​-‏ -‮⁠-⁤﻿]")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def sanitize(raw: str | None, *, html: bool = False) -> str:
    """Inert, bounded plain text: HTML to text, NFKC, control chars stripped, <= 8000 chars."""
    if not raw:
        return ""
    text = str(raw)[:MAX_INPUT]
    if html:
        p = _TextExtractor()
        try:
            p.feed(text)
            p.close()
        except Exception:  # malformed markup must never raise
            pass
        text = "".join(p.parts)
    else:
        text = _html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = _CTRL_RE.sub("", text)
    text = text.replace("<", " ").replace(">", " ")  # nothing tag-like survives
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:MAX_TEXT]


# --------------------------------------------------------------------------- coordinates

_HEM_LAT = r"([NS])"
_HEM_LON = r"([EW])"
_SEP = r"[\s,/;:]{0,4}"

_HYPHEN = re.compile(
    r"(?<![\d.\-])(\d{1,2})-(\d{2})(?:\.(\d{1,4})|-(\d{2}(?:\.\d{1,3})?))?\s?"
    + _HEM_LAT
    + _SEP
    + r"(\d{1,3})-(\d{2})(?:\.(\d{1,4})|-(\d{2}(?:\.\d{1,3})?))?\s?"
    + _HEM_LON
)
_SYMBOL = re.compile(
    r"(?<![\d.])(\d{1,2})\s?[°˚º]\s?(\d{1,2}(?:\.\d{1,4})?)\s?['′’]"
    r"(?:\s?(\d{1,2}(?:\.\d{1,3})?)\s?(?:\"|″|''|”))?\s?"
    + _HEM_LAT
    + _SEP
    + r"(\d{1,3})\s?[°˚º]\s?(\d{1,2}(?:\.\d{1,4})?)\s?['′’]"
    r"(?:\s?(\d{1,2}(?:\.\d{1,3})?)\s?(?:\"|″|''|”))?\s?" + _HEM_LON
)
_COMPACT = re.compile(r"(?<![\d.])(\d{4}|\d{6})([NS])\s?(\d{5}|\d{7})([EW])")
_DECIMAL = re.compile(
    r"(?<![\d.\-])(\d{1,2}\.\d{1,6})\s?°?\s?"
    + _HEM_LAT
    + _SEP
    + r"(\d{1,3}\.\d{1,6})\s?°?\s?"
    + _HEM_LON
)
_ZH = re.compile(
    r"([北南])纬\s?(\d{1,2})\s?度\s?(?:(\d{1,2}(?:\.\d{1,4})?)\s?分)?\s?"
    r"(?:(\d{1,2}(?:\.\d{1,3})?)\s?秒)?[\s,，、;；和及]{0,4}"
    r"([东西])经\s?(\d{1,3})\s?度\s?(?:(\d{1,2}(?:\.\d{1,4})?)\s?分)?\s?"
    r"(?:(\d{1,2}(?:\.\d{1,3})?)\s?秒)?"
)
_QLINE = re.compile(
    r"Q\)\s?[A-Z]{4}/Q[A-Z]{4}/[A-Z]{1,3}/[A-Z]{1,6}/[A-Z]{1,2}/(\d{3})/(\d{3})/"
    r"(\d{4})([NS])(\d{5})([EW])(\d{3})"
)


def _dms(deg: str, minute: str | None, sec: str | None, hem: str, limit: int) -> float | None:
    d = float(deg)
    m = float(minute) if minute else 0.0
    s = float(sec) if sec else 0.0
    if d > limit or m >= 60 or s >= 60:
        return None
    v = d + m / 60 + s / 3600
    if v > limit:
        return None
    return -v if hem in ("S", "W", "南", "西") else v


def _frac(min_int: str, frac: str | None) -> str:
    return f"{min_int}.{frac}" if frac else min_int


def _coord_matches(text: str) -> list[tuple[int, int, float, float]]:
    """(start, end, lat, lon) for every valid coordinate pair; earlier formats win overlaps."""
    text = text[:MAX_TEXT]
    found: list[tuple[int, int, float, float]] = []

    def add(m: re.Match[str], lat: float | None, lon: float | None) -> None:
        if lat is None or lon is None or len(found) >= MAX_COORDS:
            return
        if any(m.start() < e and s < m.end() for s, e, _, _ in found):
            return
        found.append((m.start(), m.end(), lat, lon))

    for m in _HYPHEN.finditer(text):
        g = m.groups()
        lat = _dms(g[0], _frac(g[1], g[2]), g[3], g[4], 90)
        lon = _dms(g[5], _frac(g[6], g[7]), g[8], g[9], 180)
        add(m, lat, lon)
    for m in _SYMBOL.finditer(text):
        g = m.groups()
        add(m, _dms(g[0], g[1], g[2], g[3], 90), _dms(g[4], g[5], g[6], g[7], 180))
    for m in _COMPACT.finditer(text):
        la, ns, lo, ew = m.groups()
        lat = _dms(la[:2], la[2:4], la[4:6] or None, ns, 90)
        lon = _dms(lo[:3], lo[3:5], lo[5:7] or None, ew, 180)
        add(m, lat, lon)
    for m in _DECIMAL.finditer(text):
        la, ns, lo, ew = m.groups()
        add(m, _dms(la, None, None, ns, 90), _dms(lo, None, None, ew, 180))
    for m in _ZH.finditer(text):
        g = m.groups()
        add(m, _dms(g[1], g[2], g[3], g[0], 90), _dms(g[5], g[6], g[7], g[4], 180))
    found.sort()
    return found


def parse_coords(text: str) -> list[tuple[float, float]]:
    """All valid (lat, lon) pairs in text order. Out-of-range or malformed values are skipped."""
    try:
        return [(la, lo) for _, _, la, lo in _coord_matches(text)]
    except Exception:
        return []


# --------------------------------------------------------------------------- shapes

_POLY_KW = re.compile(r"AREA BOUNDED BY|BOUNDED BY|IN AREA|POLYGON|各点连线|连线|围成", re.I)
_RADIUS_EN = re.compile(
    r"(?:WITHIN|RADIUS OF)\s{1,3}(\d{1,4}(?:\.\d{1,3})?)\s{0,2}"
    r"(NM|NAUTICAL MILES?|MILES?|KM|KILOMET(?:ER|RE)S?)\b",
    re.I,
)
_RADIUS_EN2 = re.compile(
    r"(\d{1,4}(?:\.\d{1,3})?)\s{0,2}(NM|NAUTICAL MILES?|MILES?|KM)\s{1,3}RADIUS", re.I
)
_RADIUS_ZH = re.compile(r"半径\s?(\d{1,4}(?:\.\d{1,3})?)\s?(海里|公里|千米)")


def _radius_km(value: str, unit: str) -> float:
    v = float(value)
    u = unit.upper()
    # Maritime warnings write "MILES" for nautical miles; only KM/公里/千米 are metric.
    return v if u.startswith(("KM", "KILO")) or u in ("公里", "千米") else v * KM_PER_NM


def circle_polygon(
    lat: float, lon: float, radius_km: float, n: int = CIRCLE_VERTICES
) -> dict[str, Any]:
    """Geodesic circle as a closed CCW GeoJSON Polygon with n vertices."""
    ang = radius_km / EARTH_RADIUS_KM
    p1, l1 = math.radians(lat), math.radians(lon)
    ring: list[list[float]] = []
    for i in range(n):
        brg = -2 * math.pi * i / n  # bearings decreasing -> counter-clockwise ring
        p2 = math.asin(math.sin(p1) * math.cos(ang) + math.cos(p1) * math.sin(ang) * math.cos(brg))
        l2 = l1 + math.atan2(
            math.sin(brg) * math.sin(ang) * math.cos(p1),
            math.cos(ang) - math.sin(p1) * math.sin(p2),
        )
        lon2 = (math.degrees(l2) + 540) % 360 - 180
        ring.append([round(lon2, 5), round(math.degrees(p2), 5)])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _signed_area(pts: Sequence[tuple[float, float]]) -> float:
    return (
        sum(
            pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
            for i in range(len(pts))
        )
        / 2
    )


def _polygon(coords: Sequence[tuple[float, float]]) -> dict[str, Any] | None:
    pts: list[tuple[float, float]] = []
    for c in coords:
        if not pts or c != pts[-1]:
            pts.append(c)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    if len(pts) < 3:
        return None
    xy = [(lo, la) for la, lo in pts]  # GeoJSON order
    area = _signed_area(xy)
    if area == 0:
        return None
    if area < 0:
        xy.reverse()  # RFC 7946: exterior ring counter-clockwise
    ring = [[lo, la] for lo, la in xy]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def parse_qline(text: str) -> tuple[float, float, float, int, int] | None:
    """ICAO Q-line -> (lat, lon, radius_nm, lower_ft, upper_ft)."""
    m = _QLINE.search(text[:MAX_TEXT])
    if not m:
        return None
    lo_fl, hi_fl, la, ns, lo, ew, rad = m.groups()
    lat = _dms(la[:2], la[2:4], None, ns, 90)
    lon = _dms(lo[:3], lo[3:5], None, ew, 180)
    if lat is None or lon is None:
        return None
    return lat, lon, float(int(rad)), int(lo_fl) * 100, int(hi_fl) * 100


def parse_shape(text: str) -> tuple[dict[str, Any] | None, str]:
    """(GeoJSON geometry | None, parse_status in ok|partial|no_geometry). Never raises."""
    try:
        text = text[:MAX_TEXT]
        q = parse_qline(text)
        body = _QLINE.sub(" ", text)
        matches = _coord_matches(body)
        coords = [(la, lo) for _, _, la, lo in matches]
        if len(coords) >= 3 and _POLY_KW.search(body):
            poly = _polygon(coords)
            if poly:
                return poly, "ok"
        rad = _RADIUS_EN.search(body) or _RADIUS_EN2.search(body)
        rad_km = _radius_km(rad.group(1), rad.group(2)) if rad else None
        if rad_km is None and (rz := _RADIUS_ZH.search(body)):
            rad_km, rad = _radius_km(rz.group(1), rz.group(2)), rz
        if rad_km and rad_km > 0 and matches:
            after = [m for m in matches if rad is not None and m[0] >= rad.start()]
            _, _, lat, lon = (after or matches)[0]
            return circle_polygon(lat, lon, min(rad_km, 5000.0)), "ok"
        if q and q[2] > 0 and len(coords) < 3:
            return circle_polygon(q[0], q[1], q[2] * KM_PER_NM), "ok"
        if len(coords) >= 3:
            poly = _polygon(coords)
            if poly:
                return poly, "partial"  # no polygon wording; ring order is a guess
        if len(coords) == 1:
            return {"type": "Point", "coordinates": [coords[0][1], coords[0][0]]}, "partial"
        if coords:
            return {
                "type": "MultiPoint",
                "coordinates": [[lo, la] for la, lo in coords],
            }, "partial"
    except Exception:
        pass
    return None, "no_geometry"


def centroid(geometry: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """(lat, lon) mean of the outer vertices (fine for the small areas notices describe)."""
    if not geometry:
        return None
    t = geometry.get("type")
    c: Any = geometry.get("coordinates")
    try:
        if t == "Point":
            return float(c[1]), float(c[0])
        if t == "Polygon":
            pts = c[0][:-1]
        elif t == "MultiPolygon":
            pts = c[0][0][:-1]
        else:
            pts = c
        return (
            sum(float(p[1]) for p in pts) / len(pts),
            sum(float(p[0]) for p in pts) / len(pts),
        )
    except (TypeError, IndexError, ValueError, ZeroDivisionError):
        return None


def _rings(geometry: Mapping[str, Any]) -> list[list[list[float]]]:
    t = geometry.get("type")
    c: Any = geometry.get("coordinates")
    if t == "Polygon":
        return [c[0]]
    if t == "MultiPolygon":
        return [p[0] for p in c]
    return []


def covering_cells(
    geometry: Mapping[str, Any] | None, res: int = 4, cap: int = 2000
) -> list[str] | None:
    """H3 cells covering the geometry; None if it would exceed `cap` (-> parse_status too_large)."""
    if not geometry:
        return []
    t = geometry.get("type")
    c: Any = geometry.get("coordinates")
    cells: set[str] = set()
    try:
        if t == "Point":
            return [h3_cell(c[1], c[0], res)]
        if t == "MultiPoint":
            return sorted({h3_cell(p[1], p[0], res) for p in c})
        for ring in _rings(geometry):
            lons = [p[0] for p in ring]
            lats = [p[1] for p in ring]
            h_km = (max(lats) - min(lats)) * 111.0
            coslat = max(0.05, math.cos(math.radians(sum(lats) / len(lats))))
            w_km = (max(lons) - min(lons)) * 111.0 * coslat
            if h_km * w_km / 1770.0 > cap * 4:  # r4 cell ~1770 km2; bbox pre-check bounds work
                return None
            poly = h3.LatLngPoly([(p[1], p[0]) for p in ring[:-1]])
            cells.update(h3.h3shape_to_cells(poly, res))
            cells.update(h3_cell(p[1], p[0], res) for p in ring)  # thin shapes still get cells
            if len(cells) > cap:
                return None
    except Exception:
        return []
    return sorted(cells)


# --------------------------------------------------------------------------- validity

_MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}
_DTG = re.compile(
    r"(?<!\d)(\d{2})(\d{2})(\d{2})Z(?:\s{1,3}([A-Z]{3})(?:\s{1,3}(\d{4}|\d{2}))?)?", re.I
)
_CANCEL = re.compile(r"CANCEL(?:LED)?\s+THIS\s+(?:MSG|MESSAGE|WARNING)", re.I)
_ZH_TIME = r"(\d{4}|\d{1,2})\s?时(?:\s?(\d{1,2})\s?分)?"
_ZH_VALID = re.compile(
    r"(\d{4})\s?年\s?(\d{1,2})\s?月\s?(\d{1,2})\s?日\s?"
    + _ZH_TIME
    + r"\s?[至到\-—~]\s?(?:(?:(\d{4})\s?年\s?)?(\d{1,2})\s?月\s?)?(?:(\d{1,2})\s?日\s?)?"
    + _ZH_TIME
)
_NOTAM_B = re.compile(r"\bB\)\s?(\d{10})")
_NOTAM_C = re.compile(r"\bC\)\s?(\d{10}|PERM(?:ANENT)?)(\s?EST)?", re.I)


@dataclass(frozen=True)
class Validity:
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    cancelled: bool = False
    permanent: bool = False
    estimated: bool = False


def _mk(y: int, mo: int, d: int, hh: int, mm: int, tz: timezone = UTC) -> datetime | None:
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=tz).astimezone(UTC)
    except ValueError:
        return None


def _add_month(y: int, mo: int) -> tuple[int, int]:
    return (y + 1, 1) if mo == 12 else (y, mo + 1)


def _dtg_points(text: str, ref: datetime | None) -> list[tuple[int, datetime]]:
    """[(match start, datetime)] with missing month/year taken from `ref` (issue time)."""
    out: list[tuple[int, datetime]] = []
    ref = ref or datetime.now(UTC)
    prev: datetime | None = None
    for m in _DTG.finditer(text[:MAX_TEXT]):
        dd, hh, mi = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4):
            mon = _MONTHS.get(m.group(4).upper())
            if mon is None:
                continue
            yr = int(m.group(5)) if m.group(5) else (prev.year if prev else ref.year)
            yr = yr + 2000 if yr < 100 else yr
            y2, mo2 = yr, mon
        else:
            y2, mo2 = (prev.year, prev.month) if prev else (ref.year, ref.month)
        dt = _mk(y2, mo2, dd, hh, mi)
        if dt is None:
            continue
        if prev is not None and not m.group(4) and dt < prev:  # window crosses month/year end
            ny, nm = _add_month(prev.year, prev.month)
            dt = _mk(ny, nm, dd, hh, mi) or dt
        out.append((m.start(), dt))
        prev = dt
        if len(out) >= 6:
            break
    return out


def parse_dtg(text: str, ref: datetime | None = None) -> datetime | None:
    pts = _dtg_points(text, ref)
    return pts[0][1] if pts else None


def _zh_time(t: str, mm: str | None) -> tuple[int, int]:
    if len(t) == 4:
        return int(t[:2]), int(t[2:])
    return int(t), int(mm or 0)


def _notam_dt(s: str) -> datetime | None:
    return _mk(2000 + int(s[:2]), int(s[2:4]), int(s[4:6]), int(s[6:8]), int(s[8:10]))


def parse_validity(text: str, ref: datetime | None = None) -> Validity:
    """NGA DTG (+ CANCEL THIS MSG), China `YYYY年M月D日HHMM时至HHMM时` (UTC+8), NOTAM B)/C)."""
    try:
        text = text[:MAX_TEXT]
        if m := _ZH_VALID.search(text):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            h1, mi1 = _zh_time(m.group(4), m.group(5))
            y2 = int(m.group(6)) if m.group(6) else y
            mo2 = int(m.group(7)) if m.group(7) else mo
            d2 = int(m.group(8)) if m.group(8) else d
            h2, mi2 = _zh_time(m.group(9), m.group(10))
            start = _mk(y, mo, d, h1, mi1, CST)
            end = _mk(y2, mo2, d2, h2, mi2, CST)
            if start and end and end < start and not m.group(8):
                end = end + timedelta(days=1)
            return Validity(start, end)
        b, c = _NOTAM_B.search(text), _NOTAM_C.search(text)
        if b or c:
            perm = bool(c and c.group(1).upper().startswith("PERM"))
            end = _notam_dt(c.group(1)) if c and not perm else None
            start = _notam_dt(b.group(1)) if b else None
            return Validity(start, end, False, perm, bool(c and c.group(2)))
        cancel = _CANCEL.search(text)
        pts = _dtg_points(text, ref)
        if cancel:
            after = [dt for pos, dt in pts if pos >= cancel.end()]
            return Validity(None, after[0] if after else None, cancelled=True)
        if len(pts) >= 2:
            return Validity(pts[0][1], pts[1][1])
        if len(pts) == 1:
            return Validity(pts[0][1], None)
    except Exception:
        pass
    return Validity()


# --------------------------------------------------------------------------- category / refs


def categorize(text: str, rules: Mapping[str, Sequence[str]]) -> str:
    """First category (rules order) with a keyword in text; case-insensitive; else 'other'."""
    hay = unicodedata.normalize("NFKC", text[:MAX_TEXT]).casefold()
    for cat, kws in rules.items():
        if any(k.casefold() in hay for k in kws):
            return cat
    return "other"


_REF_BAD = re.compile(r"[^A-Z0-9/ \-]")


def make_ref(*parts: object) -> str:
    """Sanitized short reference: [A-Z0-9/ -], <= 40 chars."""
    s = _REF_BAD.sub("", " ".join(str(p) for p in parts if p is not None).upper())
    return re.sub(r"\s+", " ", s).strip()[:40]


def content_hash(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- GeoEvent assembly


def build_notice_event(
    *,
    id: str,
    source: str,
    domain: Domain,
    kind: str,
    ts: datetime,
    raw_text: str,
    title: str | None,
    ref: str,
    rules: Mapping[str, Sequence[str]],
    html: bool = False,
    validity: Validity | None = None,
    status: str = "active",
    url: str | None = None,
    attrs: Mapping[str, Any] | None = None,
    geometry: dict[str, Any] | None = None,
    parse_status: str | None = None,
    res: int = 4,
    cap: int = 2000,
) -> GeoEvent:
    """One area GeoEvent per ADR-005 section A. Text is sanitized; only `ref` etc. are trusted."""
    text = sanitize(raw_text, html=html)
    if geometry is None:
        geometry, ps = parse_shape(text)
    else:
        ps = parse_status or "ok"
    v = validity if validity is not None else parse_validity(text, ts)
    cells = covering_cells(geometry, res, cap)
    if cells is None:
        cells, ps = [], "too_large"
    c = centroid(geometry)
    lat, lon = c if c else (None, None)
    a: dict[str, Any] = {
        "category": categorize(text, rules),
        "h3_r4_cells": cells,
        "parse_status": ps,
        "status": status,
        "ref": ref,
    }
    a.update(attrs or {})
    return GeoEvent(
        id=id,
        ts=ts,
        source=source,
        domain=domain,
        kind=kind,
        lat=lat,
        lon=lon,
        geometry=geometry,
        valid_from=v.valid_from,
        valid_to=v.valid_to,
        title=sanitize(title, html=html)[:300] if title else None,
        text=text or None,
        url=url,
        attrs=a,
        h3_r4=h3_cell(lat, lon, 4) if lat is not None and lon is not None else "",
        h3_r6=h3_cell(lat, lon, 6) if lat is not None and lon is not None else "",
    )
