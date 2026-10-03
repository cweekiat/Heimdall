"""Military aircraft type reference (task 047): static background per ICAO type designator.

`config/mil_types.yaml` holds rounded public figures (one Wikipedia link per type). Shown when an
aircraft is investigated; never used by detectors.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PATH = Path(__file__).resolve().parents[1] / "config" / "mil_types.yaml"
AAR_WORDS = {
    "gives": "refuels other aircraft",
    "receives": "can be refuelled in flight",
    "both": "refuels others and can be refuelled",
    "none": "no air-to-air refuelling",
}


@lru_cache(maxsize=1)
def table(path: Path = PATH) -> dict[str, dict[str, Any]]:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    types = raw.get("types") or {}
    return {str(k).upper(): dict(v) for k, v in types.items() if isinstance(v, dict)}


def lookup(type_code: str | None) -> dict[str, Any] | None:
    """Reference card for a type code, or None. Adds the Wikipedia URL and refuelling words."""
    if not type_code:
        return None
    t = table().get(type_code.upper())
    if t is None:
        return None
    out = {
        "type": type_code.upper(),
        "name": t.get("n"),
        "mission": t.get("m"),
        "range_km": t.get("range_km"),
        "endurance_h": t.get("end_h"),
        "crew": t.get("crew"),
        "refuelling": AAR_WORDS.get(str(t.get("aar", "none"))),
        "capabilities": t.get("cap"),
        "source": f"https://en.wikipedia.org/wiki/{t['w']}" if t.get("w") else None,
    }
    return out
