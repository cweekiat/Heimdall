"""World news headlines (task 049): a display-only ticker. Never parsed into alerts or AI input.

Feeds approved by the user 2026-10-03: BBC World and Al Jazeera RSS (AP is behind a bot challenge
and is not used). Stored per item: headline (cleaned, <= 200 chars), link, feed name, publish time.
Headlines are untrusted text: the UI writes them as text only, the shift brief never reads them.
XML with a DOCTYPE / ENTITY (entity expansion) or over `MAX_BYTES` is rejected.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from heimdall.config import Settings
from heimdall.ingest.poll import PollConnector, Unit
from heimdall.ports import Connector
from heimdall.schemas import Domain, GeoEvent, SourceReliability

MAX_BYTES = 2 << 20
KIND = "news_item"


def _clean(v: str | None, n: int = 200) -> str:
    return " ".join("".join(ch for ch in (v or "") if ch.isprintable()).split())[:n]


def parse_rss(body: bytes | str, feed: str, now: datetime) -> tuple[list[GeoEvent], int]:
    raw = body.encode() if isinstance(body, str) else body
    if len(raw) > MAX_BYTES or b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw:
        raise ValueError("unsafe or oversized XML")
    root = ET.fromstring(raw)
    out: list[GeoEvent] = []
    dropped = 0
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        if not title or not link.startswith("https://"):
            dropped += 1
            continue
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(UTC)
        except (TypeError, ValueError):
            ts = now
        out.append(
            GeoEvent(
                id=f"news|{hashlib.sha1(link.encode()).hexdigest()[:20]}",
                ts=ts,
                source="news_rss",
                domain=Domain.EVENTS,
                kind=KIND,
                valid_from=ts,
                valid_to=ts + timedelta(days=2),
                title=title,
                url=link[:500],
                attrs={"role": "context", "feed": feed, "untrusted": True},
            )
        )
    return out, dropped


class NewsRssConnector(PollConnector):
    domain = Domain.EVENTS

    def __init__(self, name: str, feeds: Mapping[str, str], **kw: Any) -> None:
        super().__init__(name, **kw)
        self.feeds = {str(k): str(v) for k, v in feeds.items()}

    def units(self) -> Sequence[Unit]:
        def one(feed: str, url: str) -> Unit:
            async def run() -> tuple[list[GeoEvent], int]:
                r = await self.client.get(url)
                return parse_rss(r.content, feed, self._now())

            return run

        return [one(f, u) for f, u in self.feeds.items()]


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> Connector:
    return NewsRssConnector(
        key,
        dict(cfg.get("feeds") or {}),
        poll_s=float(cfg.get("poll_s", 900)),
        rps=float(cfg.get("rps", 0.2)),
        reliability=SourceReliability(cfg.get("reliability", "C")),
        expected_latency_s=int(cfg.get("latency_s", 3600)),
    )
