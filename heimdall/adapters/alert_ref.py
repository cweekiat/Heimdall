"""Alert ref helpers: `HMD-YYYYMMDD-NNNN`, NNNN = per-UTC-day first-seen counter.

Pure functions shared by every Store adapter; the counter itself lives in each store.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

REF_RE = re.compile(r"^HMD-(\d{8})-(\d{4,})$")


def ref_day(first_seen: datetime) -> str:
    """UTC calendar day (YYYYMMDD) of the first detection; naive datetimes are taken as UTC."""
    ts = first_seen.replace(tzinfo=UTC) if first_seen.tzinfo is None else first_seen
    return ts.astimezone(UTC).strftime("%Y%m%d")


def format_ref(day: str, n: int) -> str:
    return f"HMD-{day}-{n:04d}"


def parse_ref(ref: str | None) -> tuple[str, int] | None:
    """(day, n) of a well-formed ref, else None."""
    m = REF_RE.match(ref or "")
    return (m.group(1), int(m.group(2))) if m else None
