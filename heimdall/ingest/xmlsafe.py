"""Parse untrusted XML from feeds (task 066). Shared by news_rss, pskreporter and sentinel1_rfi.

Feeds never need a DTD, so any `<!DOCTYPE` or `<!ENTITY` anywhere in the body is refused before
parsing. That removes entity expansion ("billion laughs", quadratic blowup) and external entities,
which is what `defusedxml` guards against, without a new dependency. Expat (>= 2.4) adds its own
amplification limits, and the stdlib parser never fetches external DTDs. Bodies are size-capped.
"""

from __future__ import annotations

import re

# Only used through parse() below (bandit B405/B314 reviewed in 066).
import xml.etree.ElementTree as ET  # nosec B405

# Case-insensitive and whitespace-tolerant, over the whole body (a DTD can follow a long prolog).
_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def parse(body: bytes | str, max_bytes: int) -> ET.Element:
    """Root element of `body`. Raises ValueError if oversized or it declares a DTD/entity,
    ET.ParseError if malformed."""
    raw = body.encode() if isinstance(body, str) else body
    if len(raw) > max_bytes or _DTD.search(raw):
        raise ValueError("unsafe or oversized XML")
    return ET.fromstring(raw)  # nosec B314
