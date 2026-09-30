"""China MSA navigation warnings (航行警告): listing page -> detail pages -> area GeoEvents.

The site geo-blocks non-China IPs (HTTP 403 -> status=down, detail "geo-blocked"); ship disabled in
config/sources.yaml. No translation: attrs.lang="zh". Only same-host links from the listing page
are fetched; URLs inside notice text are never followed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from heimdall.config import Settings
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.notice_base import NoticeConnector, load_warnings_cfg
from heimdall.ingest.notice_parse import (
    build_notice_event,
    content_hash,
    make_ref,
    parse_validity,
    sanitize,
)
from heimdall.schemas import Domain, GeoEvent, SourceReliability

DETAIL_PATH_MARK = "info_detail"  # listing hrefs we accept as notice detail pages


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._buf)))
            self._href = None


def listing_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute detail url, sanitized link text) for same-host detail links, in page order."""
    p = _LinkParser()
    try:
        p.feed(html[:500_000])
        p.close()
    except Exception:
        pass
    host = urlparse(base_url).netloc
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, text in p.links:
        url = urljoin(base_url + "/", href.strip())
        u = urlparse(url)
        if u.scheme not in ("http", "https") or u.netloc != host or DETAIL_PATH_MARK not in u.path:
            continue
        if url not in seen:
            seen.add(url)
            out.append((url, sanitize(text)[:200]))
    return out


class ChinaMsaConnector(NoticeConnector):
    def __init__(
        self,
        base_url: str,
        channels: Mapping[str, str],
        rules: Mapping[str, list[str]],
        *,
        list_path: str = "/msacncms_wap/pages/info_warn.do",
        max_details: int = 20,
        client: RateLimitedClient | None = None,
        rps: float = 0.2,
        reliability: SourceReliability = SourceReliability.B,
        expected_latency_s: int = 3600,
        poll_interval_s: float = 3600.0,
        enabled: bool = True,
        **kw: Any,
    ) -> None:
        super().__init__(
            "china_msa",
            Domain.SEA,
            reliability,
            expected_latency_s,
            client=client or RateLimitedClient("china_msa", rps=rps),
            poll_interval_s=poll_interval_s,
            geo_blocked_codes=(403, 451),
            enabled=enabled,
            **kw,
        )
        self.base_url = base_url.rstrip("/")
        self.channels = dict(channels)
        self.rules = rules
        self.list_path = list_path
        self.max_details = max_details
        self._detail_done: set[str] = set()

    async def _fetch(self) -> list[GeoEvent]:
        out: list[GeoEvent] = []
        for office, channel_id in self.channels.items():
            resp = await self.client.get(
                f"{self.base_url}{self.list_path}", params={"channelId": channel_id}
            )
            links = [
                (u, t)
                for u, t in listing_links(resp.text, self.base_url)
                if u not in self._detail_done
            ]
            for url, link_text in links[: self.max_details]:
                d = await self.client.get(url)
                self._detail_done.add(url)
                out.append(self.parse_detail(d.text, url, link_text, office))
        return out

    def parse_detail(self, html: str, url: str, link_text: str, office: str) -> GeoEvent:
        text = sanitize(html, html=True)
        qs = parse_qs(urlparse(url).query)
        raw_id = (qs.get("id") or [content_hash(url)])[0]
        ident = make_ref(raw_id).replace(" ", "")[:24] or content_hash(url)
        issued = datetime.now(UTC)
        v = parse_validity(text, issued)
        return build_notice_event(
            id=f"china_msa:{ident}",
            source="china_msa",
            domain=Domain.SEA,
            kind="nav_warning",
            ts=v.valid_from or issued,
            raw_text=text,
            title=link_text,
            ref=make_ref("MSA", office, ident)[:40],
            rules=self.rules,
            validity=v,
            url=url,
            attrs={"lang": "zh", "office": make_ref(office)},
        )


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> ChinaMsaConnector:
    w = load_warnings_cfg()
    c = w.get("china_msa", {})
    rps = float(cfg.get("rps", 0.2))
    return ChinaMsaConnector(
        cfg.get("base", "https://www.msa.gov.cn"),
        c.get("channels", {}),
        w.get("categories", {}),
        list_path=c.get("list_path", "/msacncms_wap/pages/info_warn.do"),
        max_details=int(c.get("max_details_per_poll", 20)),
        client=RateLimitedClient(key, rps=rps),
        reliability=SourceReliability(cfg.get("reliability", "B")),
        expected_latency_s=int(cfg.get("latency_s", 3600)),
        enabled=cfg.get("enabled", True) is not False,
    )
