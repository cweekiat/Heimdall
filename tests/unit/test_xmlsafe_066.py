"""066: untrusted XML guard shared by the feed parsers (offline)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from heimdall.ingest import xmlsafe
from heimdall.ingest.news_rss import parse_rss
from heimdall.ingest.notice_parse import _CTRL_RE

LAUGHS = b'<!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><r>&b;</r>'


def test_plain_xml_parses() -> None:
    assert xmlsafe.parse(b"<r><x>1</x></r>", 1000).findtext("x") == "1"
    assert xmlsafe.parse("<r/>", 1000).tag == "r"


@pytest.mark.parametrize(
    "body",
    [
        LAUGHS,
        b"<!-- " + b"x" * 8000 + b" -->" + LAUGHS,  # DTD after a long prolog (old check: 4 KB only)
        b'<!doctype r SYSTEM "http://evil.example/x.dtd"><r/>',  # lowercase, external DTD
        b"<!  DOCTYPE r><r/>",
        b'<r><!ENTITY x "y"></r>',
    ],
)
def test_dtd_and_entities_refused(body: bytes) -> None:
    with pytest.raises(ValueError):
        xmlsafe.parse(body, 1_000_000)


def test_size_cap_and_malformed() -> None:
    with pytest.raises(ValueError):
        xmlsafe.parse(b"<r>" + b"x" * 100 + b"</r>", 50)
    with pytest.raises(ET.ParseError):
        xmlsafe.parse(b"<r>", 1000)


def test_feed_parser_uses_guard() -> None:
    from datetime import UTC, datetime

    with pytest.raises(ValueError):
        parse_rss(b"<!-- " + b"x" * 8000 + b" -->" + LAUGHS, "bbc", datetime.now(UTC))


def test_notice_ctrl_regex_is_ascii_source() -> None:
    import inspect
    import unicodedata

    import heimdall.ingest.notice_parse as m

    src = inspect.getsource(m)
    # no invisible format characters (bidi overrides, zero-width) in source (B613 / Trojan Source)
    assert not [c for c in src if unicodedata.category(c) == "Cf"]
    assert _CTRL_RE.search("a\u202eb") and _CTRL_RE.search("\ufeff") and not _CTRL_RE.search("ab")
