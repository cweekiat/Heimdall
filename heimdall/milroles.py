"""Military aircraft identity and role (task 039). Pure: no I/O.

Role comes from the plane-alert-db entry when there is one (category and tags, curated per
airframe), otherwise from the ICAO type code. Strings from plane-alert-db are community data:
shown to analysts, never interpreted.

Roles: tanker, bomber, isr (surveillance / reconnaissance / AEW / maritime patrol), c2 (airborne
command posts: E-4B, E-6B), airlift, fighter, special (special forces, gunships), uav, rotary,
gov_vip (heads of state, governments, "dictator alert"), trainer, other.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ROLES = (
    "tanker",
    "bomber",
    "isr",
    "c2",
    "airlift",
    "fighter",
    "special",
    "uav",
    "rotary",
    "gov_vip",
    "trainer",
    "other",
)
#: roles that matter for build-up detection (strategic movements)
STRATEGIC = ("tanker", "bomber", "isr", "c2", "airlift")
#: "high interest": rare, each sighting is worth a note
HIGH_INTEREST_ROLES = ("bomber", "c2")

TYPE_ROLE: dict[str, str] = {
    code: role
    for role, codes in (
        # tankers; A332 / B762 / DC10 only via the plane-alert tag (airliner types are ambiguous)
        ("tanker", "K35R K35E KC46 IL78 KC10"),
        ("bomber", "B52 B1 B2 TU95 T160 TU22"),
        ("c2", "E4 E6"),  # E-4B national airborne operations centre, E-6B TACAMO ("doomsday")
        ("isr", "R135 U2 E3TF E3CF E8 E737 E7 P8 P1 P3 E2 A50 IL20 IL38 Q4"),
        (
            "airlift",
            "C17 C5M C5 C130 C30J A400 IL76 C295 CN35 KC39 Y20 AN12 AN32 A124 AN22 KC2 C27J",
        ),
        (
            "fighter",
            "F16 F15 F18H F18S F22 F35 EUFI TOR RFAL SU27 SU30 SU34 SU35 MG29 MG31 J10 A10 M2000",
        ),
        ("uav", "Q1 Q9 BTB2 UAV DRON HRON"),
        (
            "rotary",
            "H60 H47 V22 AS65 NH90 H53S H53 UH1 EC35 EC45 A139 A169 B412 B212 AS32 EC25 "
            "MI8 MI24 LYNX H64",
        ),
        ("trainer", "PC21 PC7 PC9 TEX2 T6 T38 M346 HAWK G12T L39 K8 T50 TUCA"),
    )
    for code in codes.split()
}
TAG_ROLE: dict[str, str] = {
    "Air2Air": "tanker",
    "Tactical Airlift": "airlift",
    "Cargo": "airlift",
    "Absolute Unit": "airlift",
    "Eye In The Sky": "isr",
    "Maritime Patrol": "isr",
    "Danger Zone": "fighter",
    "Jet Fighter": "fighter",
    "Warthog": "fighter",
    "I Feel The Need....": "fighter",
    "Get To The Choppa": "rotary",
    "Chopper": "rotary",
    "Choppa": "rotary",
    "Air Cavalry": "rotary",
    "Tiltrotor": "rotary",
    "UAV": "uav",
    "Training Wheels": "trainer",
    "Basic Trainer": "trainer",
    "ELINT": "isr",
    "SIGINT": "isr",
    "Electonic Reccon": "isr",  # sic, as spelled in plane-alert-db
    "Reconnaissance": "isr",
    "Surveillance": "isr",
    "Troop Transport": "airlift",
    "Military Transport": "airlift",
    "VIP Transport": "gov_vip",
    "VIP": "gov_vip",
}
#: airframe names from plane-alert-db, for military variants of airliner types (E-4B is a B742)
NAME_ROLE: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.I), r)
    for p, r in (
        (r"\bE-4B\b|Nightwatch|\bE-6B?\b|Mercury|TACAMO", "c2"),
        (r"\bKC-(135|46|10|767|30)|MRTT|Pegasus|Stratotanker|Voyager", "tanker"),
        (
            r"\bRC-135|Rivet Joint|Airseeker|Combat Sent|Cobra Ball|\bU-2|Dragon Lady|"
            r"Global Hawk|\bE-3[A-G]?\b|Sentry|\bE-8|\bP-8|Poseidon|\bE-7|Wedgetail",
            "isr",
        ),
        (r"\bB-2[A]? Spirit|\bB-52|Stratofortress|\bB-1B|Lancer", "bomber"),
        (r"\bC-17|Globemaster|\bC-5M?\b|Galaxy", "airlift"),
    )
)
CATEGORY_ROLE: dict[str, str] = {
    "Head of State": "gov_vip",
    "Dictator Alert": "gov_vip",
    "Governments": "gov_vip",
    "Royal Aircraft": "gov_vip",
    "Special Forces": "special",
    "Gunship": "special",
    "UAV": "uav",
    "Oxcart": "isr",  # plane-alert-db's spy / reconnaissance category
}

# ICAO 24-bit address blocks (ICAO Annex 10 Vol III, Table 9-1) for the countries that matter here
_BLOCKS: tuple[tuple[int, int, str], ...] = tuple(
    (int(a, 16), int(b, 16), c)
    for a, b, c in (
        ("A00000", "AFFFFF", "United States"),
        ("400000", "43FFFF", "United Kingdom"),
        ("380000", "3BFFFF", "France"),
        ("3C0000", "3FFFFF", "Germany"),
        ("300000", "33FFFF", "Italy"),
        ("340000", "37FFFF", "Spain"),
        ("100000", "1FFFFF", "Russia"),
        ("780000", "7BFFFF", "China"),
        ("840000", "87FFFF", "Japan"),
        ("7C0000", "7FFFFF", "Australia"),
        ("800000", "83FFFF", "India"),
        ("C00000", "C3FFFF", "Canada"),
        ("480000", "487FFF", "Netherlands"),
        ("448000", "44FFFF", "Belgium"),
        ("488000", "48FFFF", "Poland"),
        ("4B8000", "4BFFFF", "Turkey"),
        ("738000", "73FFFF", "Israel"),
        ("710000", "717FFF", "Saudi Arabia"),
        ("896000", "896FFF", "UAE"),
        ("730000", "737FFF", "Iran"),
        ("718000", "71FFFF", "South Korea"),
        ("720000", "727FFF", "North Korea"),
        ("899000", "8997FF", "Taiwan"),
        ("768000", "76FFFF", "Singapore"),
        ("750000", "757FFF", "Malaysia"),
        ("8A0000", "8AFFFF", "Indonesia"),
        ("758000", "75FFFF", "Philippines"),
        ("880000", "887FFF", "Thailand"),
        ("888000", "88FFFF", "Vietnam"),
        ("760000", "767FFF", "Pakistan"),
        ("704000", "704FFF", "Myanmar"),
        ("010000", "017FFF", "Egypt"),
        ("06A000", "06AFFF", "Qatar"),
        ("706000", "706FFF", "Kuwait"),
        ("70C000", "70C3FF", "Oman"),
        ("894000", "894FFF", "Bahrain"),
        ("728000", "72FFFF", "Iraq"),
        ("740000", "747FFF", "Jordan"),
        ("4A8000", "4AFFFF", "Sweden"),
        ("478000", "47FFFF", "Norway"),
        ("458000", "45FFFF", "Denmark"),
        ("460000", "467FFF", "Finland"),
        ("468000", "46FFFF", "Greece"),
        ("490000", "497FFF", "Portugal"),
        ("4B0000", "4B7FFF", "Switzerland"),
        ("440000", "447FFF", "Austria"),
        ("498000", "49FFFF", "Czechia"),
        ("470000", "477FFF", "Hungary"),
        ("4A0000", "4A7FFF", "Romania"),
        ("508000", "50FFFF", "Ukraine"),
        ("4CA000", "4CAFFF", "Ireland"),
        ("E40000", "E7FFFF", "Brazil"),
        ("C80000", "C87FFF", "New Zealand"),
        ("0D0000", "0D7FFF", "Mexico"),
        ("E00000", "E3FFFF", "Argentina"),
        ("008000", "00FFFF", "South Africa"),
    )
)


def country_of(hex_: str) -> str | None:
    """Country whose ICAO address block holds `hex_` (None if unknown or non-ICAO '~' address)."""
    try:
        v = int(hex_, 16)
    except ValueError:
        return None
    for lo, hi, c in _BLOCKS:
        if lo <= v <= hi:
            return c
    return None


@dataclass(frozen=True)
class MilIdent:
    role: str
    known: bool  # in plane-alert-db
    cmpg: str | None = None  # Civ / Mil / Pol / Gov
    category: str | None = None
    operator: str | None = None
    type_name: str | None = None
    registration: str | None = None
    country: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def high_interest(self) -> bool:
        return self.role in HIGH_INTEREST_ROLES or self.category in (
            "Head of State",
            "Dictator Alert",
            "Nuclear",
        )


def _clean(v: object, n: int = 80) -> str | None:
    """Community text: printable, trimmed, bounded. Never used as an instruction."""
    if not isinstance(v, str):
        return None
    s = "".join(ch for ch in v if ch.isprintable()).strip()
    return s[:n] or None


def classify(hex_: str, type_code: str | None, entry: Mapping[str, str] | None = None) -> MilIdent:
    """Identity and role of one aircraft; `entry` = its plane-alert-db row if any."""
    tc = (type_code or (entry or {}).get("icao_type") or "").upper() or None
    role: str | None = None
    tags: tuple[str, ...] = ()
    if entry:
        tags = tuple(t for t in (_clean(entry.get(k), 40) for k in ("tag1", "tag2", "tag3")) if t)
        cat = _clean(entry.get("category"), 40)
        role = CATEGORY_ROLE.get(cat or "")
        cmpg = _clean(entry.get("cmpg"), 8)
        if role is None and cmpg == "Gov":
            role = "gov_vip"
        if role is None and cmpg in ("Pol", "Civ"):  # police "surveillance" is not military ISR
            role = TYPE_ROLE.get(tc or "") if tc in TYPE_ROLE else "other"
        if role is None:
            # type first for unambiguous military types (a C-130 tagged "Air2Air" is a KC-130)
            role = next((TAG_ROLE[t] for t in tags if t in TAG_ROLE), None)
            if role is None or (tc in TYPE_ROLE and role not in ("tanker", "isr")):
                role = TYPE_ROLE.get(tc or "", role)
    if entry:
        name = _clean(entry.get("type")) or ""
        by_name = next((r for pat, r in NAME_ROLE if pat.search(name)), None)
        # command posts and bombers by name win over any tag (an E-4B is also "Gov"-tagged)
        if by_name in ("c2", "bomber") or (by_name and role in (None, "other")):
            role = by_name
    if role is None:
        role = TYPE_ROLE.get(tc or "", "other")
    return MilIdent(
        role=role,
        known=entry is not None,
        cmpg=_clean((entry or {}).get("cmpg"), 8),
        category=_clean((entry or {}).get("category"), 40),
        operator=_clean((entry or {}).get("operator")),
        # the list's type name only when it agrees with the broadcast type code
        type_name=_clean((entry or {}).get("type"))
        if not type_code or (entry or {}).get("icao_type", "").upper() in ("", type_code.upper())
        else None,
        # placeholders such as "@@@@@@@@" (unknown registration) are dropped
        registration=reg
        if (reg := _clean((entry or {}).get("registration"), 16))
        and any(ch.isalnum() for ch in reg)
        else None,
        country=country_of(hex_),
        tags=tags,
    )
